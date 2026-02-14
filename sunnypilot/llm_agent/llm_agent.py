#!/usr/bin/env python3
import base64
import io
import os
import subprocess
import time
from datetime import datetime

import requests
from PIL import Image
from msgq.visionipc import VisionIpcClient, VisionStreamType
import cereal.messaging as messaging

from openpilot.common.params import Params
from openpilot.common.realtime import Ratekeeper
from openpilot.common.swaglog import cloudlog
from openpilot.system.camerad.snapshot import extract_image

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = "gpt-4o-mini"
OPENAI_TIMEOUT_S = 15
OPENAI_PING_INTERVAL_S = 10
JPEG_MAX_SIZE = (960, 540)
JPEG_QUALITY = 70
LOCAL_LOG_PATH = "/data/llm-agent-test/llm_agent_runtime.log"
CAPTURE_DIR = "/data/llm-agent-test/captures"


def _log_local(message: str) -> None:
  try:
    os.makedirs(os.path.dirname(LOCAL_LOG_PATH), exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOCAL_LOG_PATH, "a", encoding="utf-8") as f:
      f.write(f"{ts} {message}\n")
  except Exception:
    pass


def _get_route_iface() -> str:
  try:
    out = subprocess.check_output(["ip", "route", "get", "1.1.1.1"], text=True, timeout=1.0).strip()
    parts = out.split()
    if "dev" in parts:
      idx = parts.index("dev")
      if idx + 1 < len(parts):
        return parts[idx + 1]
  except Exception:
    pass
  return "unknown"


def _read_api_key(params: Params) -> str:
  key = params.get("AgentApiKey")
  if key:
    if isinstance(key, bytes):
      return key.decode("utf-8").strip()
    return str(key).strip()
  return os.getenv("OPENAI_API_KEY", "").strip()


def _capture_front_camera_jpeg_b64() -> tuple[str, int, str] | None:
  stream = VisionStreamType.VISION_STREAM_ROAD
  available = VisionIpcClient.available_streams("camerad", block=False)
  if stream not in available:
    if VisionStreamType.VISION_STREAM_WIDE_ROAD in available:
      stream = VisionStreamType.VISION_STREAM_WIDE_ROAD
    else:
      return None

  # Camerad can be briefly unavailable or return no frame; retry a few times.
  for _ in range(8):
    client = VisionIpcClient("camerad", stream, True)
    deadline = time.monotonic() + 0.4
    while time.monotonic() < deadline and not client.connect(False):
      time.sleep(0.03)

    if client.is_connected() and client.num_buffers:
      buf = client.recv()
      if buf is not None:
        rgb = extract_image(buf)
        img = Image.fromarray(rgb)
        img.thumbnail(JPEG_MAX_SIZE)

        with io.BytesIO() as out:
          img.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
          jpeg_bytes = out.getvalue()
          os.makedirs(CAPTURE_DIR, exist_ok=True)
          stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
          capture_path = os.path.join(CAPTURE_DIR, f"{stamp}.jpg")
          with open(capture_path, "wb") as f:
            f.write(jpeg_bytes)
          return base64.b64encode(jpeg_bytes).decode("utf-8"), len(jpeg_bytes), capture_path

    time.sleep(0.05)

  return None


def _openai_vision_describe(api_key: str, image_b64: str) -> tuple[bool, str]:
  headers = {
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json",
  }
  payload = {
    "model": OPENAI_MODEL,
    "messages": [
      {
        "role": "system",
        "content": "You are a driving safety assistant. Be concise, factual, and road-focused.",
      },
      {
        "role": "user",
        "content": [
          {"type": "text", "text": "In exactly one sentence, start with 'Pay attention to' and describe the most relevant immediate road safety risk. If no clear risk, mention lane/traffic state briefly."},
          {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
        ],
      },
    ],
    "max_tokens": 48,
    "temperature": 0,
  }

  r = requests.post(OPENAI_CHAT_URL, headers=headers, json=payload, timeout=OPENAI_TIMEOUT_S)
  if r.status_code < 200 or r.status_code >= 300:
    return False, f"http {r.status_code}"

  body = r.json()
  content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
  content = str(content).strip().replace("\n", " ")
  if len(content) > 80:
    content = content[:80]
  return True, content or "ok"


def main():
  params = Params()
  sm = messaging.SubMaster(['deviceState'])
  cloudlog.info("llm-agent: starting")
  rk = Ratekeeper(1.0)
  last_heartbeat = 0.0
  last_ping = 0.0
  warned_no_key = False

  while True:
    sm.update(0)
    now = time.monotonic()
    if now - last_heartbeat >= 60.0:
      enabled = params.get_bool("LLMAgentEnabled")
      cloudlog.info(f"llm-agent: alive (enabled={enabled})")
      last_heartbeat = now

    if now - last_ping >= OPENAI_PING_INTERVAL_S:
      api_key = _read_api_key(params)
      if not api_key:
        if not warned_no_key:
          cloudlog.warning("llm-agent: no API key set (AgentApiKey or OPENAI_API_KEY)")
          warned_no_key = True
      else:
        warned_no_key = False
        try:
          network_type = sm['deviceState'].networkType
          route_iface = _get_route_iface()
          cloudlog.info(f"llm-agent: vision attempt (networkType={network_type}, routeIface={route_iface})")
          _log_local(f"vision attempt networkType={network_type} routeIface={route_iface}")
          image_payload = _capture_front_camera_jpeg_b64()
          if not image_payload:
            cloudlog.warning("llm-agent: no front camera frame available from camerad")
            _log_local("no front camera frame available from camerad")
          else:
            image_b64, image_size, capture_path = image_payload
            cloudlog.info(f"llm-agent: encoded frame size={image_size}B")
            _log_local(f"encoded frame size={image_size}B capture={capture_path}")
            ok, detail = _openai_vision_describe(api_key, image_b64)
            if ok:
              cloudlog.info(f"llm-agent: road summary: {detail}")
              _log_local(f"road summary: {detail}")
            else:
              cloudlog.warning(f"llm-agent: OpenAI vision failed ({detail})")
              _log_local(f"OpenAI vision failed ({detail})")
        except Exception as e:
          cloudlog.warning(f"llm-agent: OpenAI request error: {e}")
          _log_local(f"OpenAI request error: {e}")
      last_ping = now

    rk.keep_time()


if __name__ == "__main__":
  main()
