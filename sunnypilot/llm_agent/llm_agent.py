#!/usr/bin/env python3
import base64
import io
import os
import subprocess
import time
import wave
from datetime import datetime

import requests
from PIL import Image
from msgq.visionipc import VisionIpcClient, VisionStreamType
import cereal.messaging as messaging

from openpilot.common.params import Params
from openpilot.common.realtime import Ratekeeper
from openpilot.common.swaglog import cloudlog
from openpilot.system.camerad.snapshot import extract_image


def _env_float(name: str, default: float) -> float:
  try:
    return float(os.getenv(name, str(default)))
  except ValueError:
    return default


OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_TRANSCRIBE_URL = "https://api.openai.com/v1/audio/transcriptions"
OPENAI_MODEL = os.getenv("LLM_AGENT_VISION_MODEL", "gpt-4o")
OPENAI_AUDIO_MODEL = "gpt-4o-mini-transcribe"
OPENAI_TIMEOUT_S = 25
OPENAI_PING_INTERVAL_S = _env_float("LLM_AGENT_VISION_INTERVAL_S", 3)
OPENAI_AUDIO_TIMEOUT_S = 20
OPENAI_MAX_COMPLETION_TOKENS = 256
ADVISORY_HOLD_S = _env_float("LLM_AGENT_ADVISORY_HOLD_S", 5)
JPEG_MAX_SIZE = (960, 540)
ROAD_CROP_SIZE = (960, 540)
ROAD_CROP_TOP_FRACTION = 0.40
ROAD_CROP_BOTTOM_FRACTION = 0.98
JPEG_QUALITY = 70
try:
  RESAMPLE_LANCZOS = Image.Resampling.LANCZOS
except AttributeError:
  RESAMPLE_LANCZOS = Image.LANCZOS
LOCAL_LOG_PATH = "/data/llm-agent-test/llm_agent_runtime.log"
CAPTURE_DIR = "/data/llm-agent-test/captures"
ADVISORY_PARAM = "LLMAgentAdvisory"
AUDIO_ENABLED_PARAM = "LLMAgentAudioEnabled"
AUDIO_TRIGGER_PARAM = "LLMAgentAudioTrigger"
AUDIO_CAPTURE_SECONDS = 2.0
AUDIO_CAPTURE_TIMEOUT_S = 8.0
VISION_SYSTEM_PROMPT = (
  "You are a transportation agency road asset inspector reviewing a forward-facing vehicle image. "
  "Focus on routine maintenance and post-hurricane windshield-survey issues. Favor recall for "
  "visible maintenance concerns that a DOT crew should review, including minor or early-stage "
  "defects near the travel lane, shoulder, curb, or drainage path. Do not invent hazards; ignore "
  "normal traffic unless it directly blocks inspection or involves road asset damage."
)
VISION_USER_PROMPT = (
  "You will receive two images from the same moment: first the full forward scene, then a lower "
  "road-surface crop emphasizing pavement, lane markings, shoulders, gutters, curbs, and drainage. "
  "Use the crop to detect small pavement and roadside asset defects while using the full image for "
  "context. "
  "Return exactly one short sentence. If a visible road asset issue exists, start with one label "
  "from PAVEMENT, FLOODING, DEBRIS, SIGN_SIGNAL, GUARDRAIL, SHOULDER, LANE_MARKING, DRAINAGE, "
  "BRIDGE, WORK_ZONE, OTHER_ASSET, then describe the issue and where it appears. Report likely "
  "maintenance issues even if they are not severe. Prioritize storm damage, standing water, gutter "
  "or curb ponding, washouts, blocked drains, downed trees or power lines, damaged signs or signals, "
  "missing or leaning barriers, potholes, pavement patches, cracking, edge drop-offs, faded or "
  "blocked lane markings, shoulder erosion, and debris. If the road/assets appear normal or the "
  "image is too unclear to inspect, return "
  "'NO_ASSET_ISSUE: no agency-actionable road asset issue visible.'"
)


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


def _jpeg_b64(img: Image.Image, capture_path: str, max_size: tuple[int, int] | None = None,
              exact_size: tuple[int, int] | None = None) -> tuple[str, int, str, tuple[int, int]]:
  out_img = img.copy()
  if exact_size is not None:
    out_img = out_img.resize(exact_size, RESAMPLE_LANCZOS)
  elif max_size is not None:
    out_img.thumbnail(max_size)

  with io.BytesIO() as out:
    out_img.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    jpeg_bytes = out.getvalue()

  with open(capture_path, "wb") as f:
    f.write(jpeg_bytes)

  return base64.b64encode(jpeg_bytes).decode("utf-8"), len(jpeg_bytes), capture_path, out_img.size


def _road_surface_crop(img: Image.Image) -> Image.Image:
  width, height = img.size
  top = int(height * ROAD_CROP_TOP_FRACTION)
  bottom = int(height * ROAD_CROP_BOTTOM_FRACTION)
  bottom = max(top + 1, min(height, bottom))
  return img.crop((0, top, width, bottom))


def _capture_front_camera_jpegs_b64() -> tuple[tuple[str, int, str, tuple[int, int]], tuple[str, int, str, tuple[int, int]]] | None:
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
        road_crop = _road_surface_crop(img)

        os.makedirs(CAPTURE_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        full_path = os.path.join(CAPTURE_DIR, f"{stamp}_full.jpg")
        crop_path = os.path.join(CAPTURE_DIR, f"{stamp}_road.jpg")
        full_payload = _jpeg_b64(img, full_path, max_size=JPEG_MAX_SIZE)
        crop_payload = _jpeg_b64(road_crop, crop_path, exact_size=ROAD_CROP_SIZE)
        return full_payload, crop_payload

    time.sleep(0.05)

  return None


def _openai_vision_describe(api_key: str, image_payloads: tuple[tuple[str, int, str, tuple[int, int]], ...]) -> tuple[bool, str]:
  headers = {
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json",
  }
  content = [{"type": "text", "text": VISION_USER_PROMPT}]
  for image_b64, _, _, _ in image_payloads:
    content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}})

  payload = {
    "model": OPENAI_MODEL,
    "messages": [
      {
        "role": "system",
        "content": VISION_SYSTEM_PROMPT,
      },
      {
        "role": "user",
        "content": content,
      },
    ],
    "max_completion_tokens": OPENAI_MAX_COMPLETION_TOKENS,
  }
  if OPENAI_MODEL.startswith("gpt-5"):
    payload["reasoning_effort"] = "minimal"

  r = requests.post(OPENAI_CHAT_URL, headers=headers, json=payload, timeout=OPENAI_TIMEOUT_S)
  if r.status_code < 200 or r.status_code >= 300:
    try:
      body = r.json()
      err = body.get("error", {})
      msg = str(err.get("message") or err.get("code") or "").strip()
      return False, f"http {r.status_code}: {msg[:80]}"
    except Exception:
      return False, f"http {r.status_code}"

  body = r.json()
  choice = body.get("choices", [{}])[0]
  content = choice.get("message", {}).get("content", "")
  content = str(content).strip().replace("\n", " ")
  if len(content) > 80:
    content = content[:80]
  if not content:
    finish_reason = choice.get("finish_reason", "unknown")
    return False, f"empty response ({finish_reason})"
  return True, content


def _to_short_advisory(summary: str) -> str:
  text = (summary or "").strip().lower()
  normalized = text.replace("_", " ")
  label = normalized.split(":", 1)[0].strip().upper().replace(" ", "_")
  label_advisories = {
    "PAVEMENT": "Pavement issue",
    "FLOODING": "Flooding",
    "DEBRIS": "Debris in roadway",
    "SIGN_SIGNAL": "Sign/signal damage",
    "GUARDRAIL": "Barrier damage",
    "SHOULDER": "Shoulder issue",
    "LANE_MARKING": "Marking issue",
    "DRAINAGE": "Drainage issue",
    "BRIDGE": "Bridge issue",
    "WORK_ZONE": "Work zone issue",
    "OTHER_ASSET": "Asset issue",
  }

  if not text or text in ("ok", "okay") or any(
    k in normalized
    for k in ("no asset issue", "no road asset issue", "no agency-actionable", "no clear issue")
  ):
    return ""
  if label in label_advisories:
    return label_advisories[label]
  if any(k in normalized for k in ("flood", "standing water", "ponding", "high water")):
    return "Flooding"
  if any(k in normalized for k in ("debris", "downed tree", "fallen tree", "power line", "object in road")):
    return "Debris in roadway"
  if any(k in normalized for k in ("drain", "culvert", "inlet", "blocked grate")):
    return "Drainage issue"
  if any(k in normalized for k in ("sign", "signal", "traffic light", "mast arm")):
    return "Sign/signal damage"
  if any(k in normalized for k in ("guardrail", "barrier", "crash attenuator")):
    return "Barrier damage"
  if any(k in normalized for k in ("lane marking", "striping", "faded marking", "blocked marking")):
    return "Marking issue"
  if any(k in normalized for k in ("shoulder", "edge drop", "drop-off", "erosion")):
    return "Shoulder issue"
  if any(k in normalized for k in ("bridge", "overpass", "approach slab")):
    return "Bridge issue"
  if any(k in normalized for k in ("work zone", "cone", "barrel", "barricade")):
    return "Work zone issue"
  if any(
    k in normalized
    for k in ("pavement", "pothole", "crack", "washout", "sinkhole", "rutting", "rough road", "broken road")
  ):
    return "Pavement issue"
  return ""


def _update_advisory(params: Params, advisory: str, active_advisory: str, advisory_expiry: float, now: float) -> tuple[str, float]:
  if advisory:
    params.put(ADVISORY_PARAM, advisory)
    return advisory, now + ADVISORY_HOLD_S

  if active_advisory and now < advisory_expiry:
    params.put(ADVISORY_PARAM, active_advisory)
    return active_advisory, advisory_expiry

  params.put(ADVISORY_PARAM, "")
  return "", 0.0


def _capture_audio_prompt_wav(sm_audio: messaging.SubMaster) -> bytes | None:
  pcm = bytearray()
  sample_rate = 16000
  target_bytes = int(AUDIO_CAPTURE_SECONDS * sample_rate * 2)
  start = time.monotonic()

  while time.monotonic() - start < AUDIO_CAPTURE_TIMEOUT_S:
    sm_audio.update(100)
    if not sm_audio.updated['rawAudioData']:
      continue

    msg = sm_audio['rawAudioData']
    chunk = bytes(msg.data)
    if not chunk:
      continue

    if int(msg.sampleRate) > 0 and int(msg.sampleRate) != sample_rate:
      sample_rate = int(msg.sampleRate)
      target_bytes = int(AUDIO_CAPTURE_SECONDS * sample_rate * 2)

    pcm.extend(chunk)
    if len(pcm) >= target_bytes:
      break

  if len(pcm) < target_bytes // 2:
    return None

  wav_io = io.BytesIO()
  with wave.open(wav_io, "wb") as wf:
    wf.setnchannels(1)
    wf.setsampwidth(2)  # int16
    wf.setframerate(sample_rate)
    wf.writeframes(bytes(pcm[:target_bytes]))
  return wav_io.getvalue()


def _openai_transcribe_audio(api_key: str, wav_bytes: bytes) -> tuple[bool, str]:
  headers = {"Authorization": f"Bearer {api_key}"}
  files = {"file": ("prompt.wav", wav_bytes, "audio/wav")}
  data = {"model": OPENAI_AUDIO_MODEL}
  r = requests.post(OPENAI_TRANSCRIBE_URL, headers=headers, files=files, data=data, timeout=OPENAI_AUDIO_TIMEOUT_S)
  if r.status_code < 200 or r.status_code >= 300:
    return False, f"http {r.status_code}"

  body = r.json()
  text = str(body.get("text", "")).strip()
  if len(text) > 120:
    text = text[:120]
  return True, text


def _to_audio_advisory(transcript: str) -> str:
  text = (transcript or "").strip().lower()
  if not text:
    return "Audio unclear"
  if any(k in text for k in ("pothole", "rough", "bump")):
    return "Pothole risk"
  if any(k in text for k in ("pedestrian", "person", "walker")):
    return "Pedestrian nearby"
  if any(k in text for k in ("bike", "cyclist")):
    return "Cyclist nearby"
  if any(k in text for k in ("stop", "brake", "slow")):
    return "Prepare to slow"
  return "Voice request received"


def main():
  params = Params()
  params.put(ADVISORY_PARAM, "")
  sm = messaging.SubMaster(['deviceState'])
  sm_audio = messaging.SubMaster(['rawAudioData'])
  cloudlog.info("llm-agent: starting")
  rk = Ratekeeper(1.0)
  last_heartbeat = 0.0
  last_ping = 0.0
  warned_no_key = False
  active_advisory = ""
  advisory_expiry = 0.0

  while True:
    sm.update(0)
    now = time.monotonic()
    api_key = _read_api_key(params)
    audio_enabled = params.get_bool(AUDIO_ENABLED_PARAM)
    audio_triggered = params.get_bool(AUDIO_TRIGGER_PARAM)
    if now - last_heartbeat >= 60.0:
      enabled = params.get_bool("LLMAgentEnabled")
      cloudlog.info(f"llm-agent: alive (enabled={enabled})")
      last_heartbeat = now

    if audio_enabled and audio_triggered:
      if not api_key:
        cloudlog.warning("llm-agent: audio trigger ignored, no API key")
        _log_local("audio trigger ignored, no API key")
      else:
        warned_no_key = False
        try:
          params.put_bool(AUDIO_TRIGGER_PARAM, False)
          cloudlog.info("llm-agent: audio trigger detected")
          _log_local("audio trigger detected")
          wav_bytes = _capture_audio_prompt_wav(sm_audio)
          if not wav_bytes:
            cloudlog.warning("llm-agent: audio capture failed")
            _log_local("audio capture failed")
          else:
            ok_audio, transcript_or_error = _openai_transcribe_audio(api_key, wav_bytes)
            if ok_audio:
              transcript = transcript_or_error
              advisory = _to_audio_advisory(transcript)
              params.put(ADVISORY_PARAM, advisory)
              cloudlog.info(f"llm-agent: audio transcript: {transcript}")
              _log_local(f"audio transcript: {transcript}")
              _log_local(f"ui advisory: {advisory}")
            else:
              cloudlog.warning(f"llm-agent: audio transcription failed ({transcript_or_error})")
              _log_local(f"audio transcription failed ({transcript_or_error})")
        except Exception as e:
          cloudlog.warning(f"llm-agent: audio request error: {e}")
          _log_local(f"audio request error: {e}")

    if now - last_ping >= OPENAI_PING_INTERVAL_S:
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
          image_payloads = _capture_front_camera_jpegs_b64()
          if not image_payloads:
            active_advisory, advisory_expiry = _update_advisory(
              params, "", active_advisory, advisory_expiry, now
            )
            cloudlog.warning("llm-agent: no front camera frame available from camerad")
            _log_local("no front camera frame available from camerad")
            _log_local(f"ui advisory: {active_advisory or 'cleared'}")
          else:
            full_payload, crop_payload = image_payloads
            _, full_size, full_path, full_dims = full_payload
            _, crop_size, crop_path, crop_dims = crop_payload
            cloudlog.info(f"llm-agent: encoded frames full={full_size}B crop={crop_size}B")
            _log_local(f"encoded frame size={full_size}B dims={full_dims[0]}x{full_dims[1]} capture={full_path}")
            _log_local(f"encoded road crop size={crop_size}B dims={crop_dims[0]}x{crop_dims[1]} capture={crop_path}")
            request_start = time.monotonic()
            ok, detail = _openai_vision_describe(api_key, image_payloads)
            request_elapsed = time.monotonic() - request_start
            response_now = time.monotonic()
            _log_local(f"vision response model={OPENAI_MODEL} elapsed={request_elapsed:.2f}s ok={ok}")
            if ok:
              advisory = _to_short_advisory(detail)
              active_advisory, advisory_expiry = _update_advisory(
                params, advisory, active_advisory, advisory_expiry, response_now
              )
              cloudlog.info(f"llm-agent: road summary: {detail}")
              _log_local(f"road summary: {detail}")
              _log_local(f"ui advisory: {active_advisory or 'cleared'}")
            else:
              active_advisory, advisory_expiry = _update_advisory(
                params, "", active_advisory, advisory_expiry, response_now
              )
              cloudlog.warning(f"llm-agent: OpenAI vision failed ({detail})")
              _log_local(f"OpenAI vision failed ({detail})")
              _log_local(f"ui advisory: {active_advisory or 'cleared'}")
        except Exception as e:
          active_advisory, advisory_expiry = _update_advisory(
            params, "", active_advisory, advisory_expiry, now
          )
          cloudlog.warning(f"llm-agent: OpenAI request error: {e}")
          _log_local(f"OpenAI request error: {e}")
          _log_local(f"ui advisory: {active_advisory or 'cleared'}")
      last_ping = now

    rk.keep_time()


if __name__ == "__main__":
  main()
