#!/usr/bin/env python3
import json
import os
import random
import threading
import time
import traceback
from collections.abc import Iterator

import requests
import cereal.messaging as messaging
from cereal import log

from openpilot.common.params import Params
from openpilot.common.realtime import set_core_affinity
from openpilot.common.swaglog import cloudlog
from openpilot.common.utils import get_upload_stream
from openpilot.system.hardware.hw import Paths
from openpilot.system.loggerd.uploader import get_directory_sort
from openpilot.system.loggerd.xattr_cache import getxattr, setxattr

NetworkType = log.DeviceState.NetworkType
UPLOAD_ATTR_NAME = "user.dropbox.upload"
UPLOAD_ATTR_VALUE = b"1"

DROPBOX_CONTENT_HOST = "https://content.dropboxapi.com"
DROPBOX_API_HOST = "https://api.dropboxapi.com"
DROPBOX_SINGLE_UPLOAD_LIMIT = 150 * 1024 * 1024
DROPBOX_CHUNK_SIZE = 8 * 1024 * 1024
DROPBOX_ACCESS_TOKEN_GRACE = 60

allow_sleep = bool(int(os.getenv("UPLOADER_SLEEP", "1")))
force_wifi = os.getenv("FORCEWIFI") is not None
fake_upload = os.getenv("FAKEUPLOAD") is not None


class FakeRequest:
  def __init__(self, content_length: int):
    self.headers = {"Content-Length": str(content_length)}


class FakeResponse:
  def __init__(self, content_length: int):
    self.status_code = 200
    self.request = FakeRequest(content_length)


def listdir_by_creation(d: str) -> list[str]:
  if not os.path.isdir(d):
    return []

  try:
    paths = [f for f in os.listdir(d) if os.path.isdir(os.path.join(d, f))]
    return sorted(paths, key=get_directory_sort)
  except OSError:
    cloudlog.exception("dropbox_listdir_by_creation_failed")
    return []


def clear_locks(root: str) -> None:
  for logdir in os.listdir(root):
    path = os.path.join(root, logdir)
    try:
      for fname in os.listdir(path):
        if fname.endswith(".lock"):
          os.unlink(os.path.join(path, fname))
    except OSError:
      cloudlog.exception("dropbox_clear_locks_failed")


class DropboxUploader:
  def __init__(self, root: str):
    self.root = root
    self.params = Params()
    self.last_filename = ""
    self.immediate_folders = ["crash/", "boot/"]
    self.immediate_priority = {"qlog": 0, "qlog.zst": 0, "qcamera.ts": 1}

    self._access_token: str | None = None
    self._access_token_expiry_mono: float = 0.0

  def get_dropbox_root(self) -> str:
    root = self.params.get("DropboxUploadFolder")
    if not root:
      return "/DoTPilotDrives"
    root = root.strip()
    if not root.startswith("/"):
      root = "/" + root
    return root.rstrip("/") or "/DoTPilotDrives"

  @staticmethod
  def _route_grouped_key(logdir: str, name: str) -> str:
    # Route segments are stored locally as "<route>--<segment>".
    # Group uploads as "<route>/<segment>/<file>" in Dropbox.
    if logdir in ("boot", "crash"):
      return os.path.join(logdir, name)

    if "--" not in logdir:
      return os.path.join(logdir, name)

    route, segment = logdir.rsplit("--", 1)
    if segment.isdigit():
      return os.path.join(route, segment, name)

    return os.path.join(logdir, name)

  def get_access_token(self) -> str | None:
    now = time.monotonic()
    if self._access_token is not None and now < self._access_token_expiry_mono:
      return self._access_token

    static_access_token = self.params.get("DropboxAccessToken")
    if static_access_token:
      self._access_token = static_access_token
      self._access_token_expiry_mono = float("inf")
      return self._access_token

    refresh_token = self.params.get("DropboxRefreshToken")
    app_key = self.params.get("DropboxAppKey")
    app_secret = self.params.get("DropboxAppSecret")
    if not refresh_token or not app_key or not app_secret:
      return None

    try:
      resp = requests.post(
        f"{DROPBOX_API_HOST}/oauth2/token",
        data={
          "grant_type": "refresh_token",
          "refresh_token": refresh_token,
          "client_id": app_key,
          "client_secret": app_secret,
        },
        timeout=10,
      )
      if resp.status_code != 200:
        cloudlog.event("dropbox_token_refresh_failed", status_code=resp.status_code, body=resp.text)
        return None

      token_data = resp.json()
      access_token = token_data.get("access_token")
      expires_in = int(token_data.get("expires_in", 0))
      if not access_token:
        cloudlog.event("dropbox_token_refresh_invalid_response", token_data=token_data)
        return None

      self._access_token = access_token
      ttl = max(0, expires_in - DROPBOX_ACCESS_TOKEN_GRACE)
      self._access_token_expiry_mono = now + ttl if ttl > 0 else now + DROPBOX_ACCESS_TOKEN_GRACE
      return self._access_token
    except Exception:
      cloudlog.exception("dropbox_token_refresh_exception")
      return None

  def list_upload_files(self, _metered: bool) -> Iterator[tuple[str, str, str]]:
    for logdir in listdir_by_creation(self.root):
      path = os.path.join(self.root, logdir)
      try:
        names = os.listdir(path)
      except OSError:
        continue

      if any(name.endswith(".lock") for name in names):
        continue

      for name in sorted(names, key=lambda n: self.immediate_priority.get(n, 1000)):
        key = self._route_grouped_key(logdir, name)
        fn = os.path.join(path, name)
        try:
          is_uploaded = getxattr(fn, UPLOAD_ATTR_NAME) == UPLOAD_ATTR_VALUE
        except OSError:
          cloudlog.event("dropbox_uploader_getxattr_failed", key=key, fn=fn)
          continue

        if is_uploaded:
          continue

        yield name, key, fn

  def next_file_to_upload(self, metered: bool) -> tuple[str, str, str] | None:
    upload_files = list(self.list_upload_files(metered))

    for name, key, fn in upload_files:
      if any(f in fn for f in self.immediate_folders):
        return name, key, fn

    for name, key, fn in upload_files:
      if name in self.immediate_priority:
        return name, key, fn

    if upload_files:
      return upload_files[0]

    return None

  @staticmethod
  def _to_dropbox_path(root: str, key: str) -> str:
    normalized_key = key.replace(os.sep, "/")
    return f"{root}/{normalized_key}"

  @staticmethod
  def _upload_single(access_token: str, stream, remote_path: str) -> requests.Response:
    headers = {
      "Authorization": f"Bearer {access_token}",
      "Content-Type": "application/octet-stream",
      "Dropbox-API-Arg": json.dumps({
        "path": remote_path,
        "mode": "overwrite",
        "autorename": False,
        "mute": True,
      }),
    }
    return requests.post(f"{DROPBOX_CONTENT_HOST}/2/files/upload", headers=headers, data=stream, timeout=60)

  @staticmethod
  def _upload_session(access_token: str, stream, content_length: int, remote_path: str) -> requests.Response:
    headers_base = {
      "Authorization": f"Bearer {access_token}",
      "Content-Type": "application/octet-stream",
    }

    first_chunk = stream.read(DROPBOX_CHUNK_SIZE)
    start_resp = requests.post(
      f"{DROPBOX_CONTENT_HOST}/2/files/upload_session/start",
      headers={**headers_base, "Dropbox-API-Arg": json.dumps({"close": False})},
      data=first_chunk,
      timeout=60,
    )
    if start_resp.status_code != 200:
      return start_resp

    session_id = start_resp.json()["session_id"]
    offset = len(first_chunk)

    while (content_length - offset) > DROPBOX_CHUNK_SIZE:
      chunk = stream.read(DROPBOX_CHUNK_SIZE)
      append_resp = requests.post(
        f"{DROPBOX_CONTENT_HOST}/2/files/upload_session/append_v2",
        headers={
          **headers_base,
          "Dropbox-API-Arg": json.dumps({
            "cursor": {"session_id": session_id, "offset": offset},
            "close": False,
          }),
        },
        data=chunk,
        timeout=60,
      )
      if append_resp.status_code != 200:
        return append_resp
      offset += len(chunk)

    final_chunk = stream.read(DROPBOX_CHUNK_SIZE)
    finish_resp = requests.post(
      f"{DROPBOX_CONTENT_HOST}/2/files/upload_session/finish",
      headers={
        **headers_base,
        "Dropbox-API-Arg": json.dumps({
          "cursor": {"session_id": session_id, "offset": offset},
          "commit": {
            "path": remote_path,
            "mode": "overwrite",
            "autorename": False,
            "mute": True,
          },
        }),
      },
      data=final_chunk,
      timeout=60,
    )
    return finish_resp

  def do_upload(self, key: str, fn: str):
    access_token = self.get_access_token()
    if access_token is None:
      cloudlog.event("dropbox_access_token_missing")
      return None

    if fake_upload:
      file_size = os.path.getsize(fn)
      return FakeResponse(file_size)

    stream = None
    try:
      compress = key.endswith(".zst") and not fn.endswith(".zst")
      stream, content_length = get_upload_stream(fn, compress)

      remote_path = self._to_dropbox_path(self.get_dropbox_root(), key)
      cloudlog.debug("dropbox_upload_target %s", remote_path)

      if content_length <= DROPBOX_SINGLE_UPLOAD_LIMIT:
        return self._upload_single(access_token, stream, remote_path)
      return self._upload_session(access_token, stream, content_length, remote_path)
    finally:
      if stream:
        stream.close()

  def upload(self, name: str, key: str, fn: str, network_type: int, metered: bool) -> bool:
    try:
      sz = os.path.getsize(fn)
    except OSError:
      cloudlog.exception("dropbox_upload_getsize_failed")
      return False

    cloudlog.event("dropbox_upload_start", key=key, fn=fn, sz=sz, network_type=network_type, metered=metered)

    if sz == 0:
      success = True
    else:
      start_time = time.monotonic()
      stat = None
      last_exc = None
      try:
        stat = self.do_upload(key, fn)
      except Exception as e:
        last_exc = (e, traceback.format_exc())

      if stat is not None and 200 <= stat.status_code < 300:
        self.last_filename = fn
        dt = time.monotonic() - start_time
        content_length = int(stat.request.headers.get("Content-Length", 0))
        speed = (content_length / 1e6) / dt if dt > 0 else 0
        cloudlog.event("dropbox_upload_success", key=key, fn=fn, sz=sz, content_length=content_length,
                       network_type=network_type, metered=metered, speed=speed)
        success = True
      else:
        success = False
        cloudlog.event("dropbox_upload_failed", stat=stat, exc=last_exc, key=key, fn=fn, sz=sz,
                       network_type=network_type, metered=metered)

    if success:
      try:
        setxattr(fn, UPLOAD_ATTR_NAME, UPLOAD_ATTR_VALUE)
      except OSError:
        cloudlog.event("dropbox_uploader_setxattr_failed", key=key, fn=fn, sz=sz)

    return success

  def step(self, network_type: int, metered: bool) -> bool | None:
    d = self.next_file_to_upload(metered)
    if d is None:
      return None

    name, key, fn = d
    if key.endswith(("qlog", "rlog")) or (key.startswith("boot/") and not key.endswith(".zst")):
      key += ".zst"

    return self.upload(name, key, fn, network_type, metered)


def main(exit_event: threading.Event | None = None) -> None:
  if exit_event is None:
    exit_event = threading.Event()

  try:
    set_core_affinity([0, 1, 2, 3])
  except Exception:
    cloudlog.exception("dropbox_uploader_set_core_affinity_failed")

  clear_locks(Paths.log_root())

  params = Params()
  sm = messaging.SubMaster(["deviceState"])
  uploader = DropboxUploader(Paths.log_root())
  last_pending_count_update = 0.0

  backoff = 0.1
  while not exit_event.is_set():
    sm.update(0)
    offroad = params.get_bool("IsOffroad")
    now = time.monotonic()
    if (now - last_pending_count_update) >= 10.0:
      pending_count = len(list(uploader.list_upload_files(False)))
      params.put("DropboxUploadPendingCount", pending_count)
      last_pending_count_update = now

    network_type = sm["deviceState"].networkType if not force_wifi else NetworkType.wifi
    # Dropbox uploader is intended for high-bandwidth Wi-Fi syncing only.
    if network_type != NetworkType.wifi:
      if allow_sleep:
        time.sleep(60 if offroad else 5)
      continue

    success = uploader.step(sm["deviceState"].networkType.raw, sm["deviceState"].networkMetered)
    if success is None:
      backoff = 60 if offroad else 5
    elif success:
      backoff = 0.1
    else:
      cloudlog.info("dropbox_upload_backoff %r", backoff)
      backoff = min(backoff * 2, 120)

    if allow_sleep:
      time.sleep(backoff + random.uniform(0, backoff))


if __name__ == "__main__":
  main()
