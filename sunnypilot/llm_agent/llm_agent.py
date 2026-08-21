#!/usr/bin/env python3
import base64
import io
import json
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
ROAD_INSPECTION_PARAM = "LLMRoadInspection"
AUDIO_ENABLED_PARAM = "LLMAgentAudioEnabled"
AUDIO_TRIGGER_PARAM = "LLMAgentAudioTrigger"
AUDIO_CAPTURE_SECONDS = 2.0
AUDIO_CAPTURE_TIMEOUT_S = 8.0
ROAD_INSPECTION_SCHEMA_VERSION = 1
MIN_DISPLAY_CONFIDENCE = 0.35
DRIVER_DISPLAY_MAX_CHARS = 72
VISION_SYSTEM_PROMPT = (
  "You are a transportation agency road asset inspector reviewing a forward-facing vehicle image. "
  "Focus on routine maintenance and post-hurricane windshield-survey issues. Favor recall for "
  "visible maintenance concerns that a DOT crew should review, including minor or early-stage "
  "defects near the travel lane, shoulder, curb, or drainage path. Return JSON only. Do not invent "
  "hazards; ignore normal traffic unless it directly blocks inspection or involves road asset damage."
)
VISION_USER_PROMPT = (
  "You will receive two images from the same moment: first the full forward scene, then a lower "
  "road-surface crop emphasizing pavement, lane markings, shoulders, gutters, curbs, and drainage. "
  "Use the crop to detect small pavement and roadside asset defects while using the full image for "
  "context. Use an FDOT Maintenance Rating Program inspired taxonomy: inspection_element must be "
  "one of Roadway, Roadside, Traffic Services, Drainage, Vegetation/Aesthetics. Prefer categories "
  "such as pothole, edge raveling, shoving, depression/bump, paved shoulder/turnout, joint/cracking, "
  "rutting, pavement marking, roadway sweeping/debris, sign/object marker, guardrail/attenuator, "
  "inlet/ditch/drain, vegetation obstruction, flooding/ponding, bridge, or work zone. Return JSON "
  "with exactly these top-level fields: found_issue boolean, inspection_element string, "
  "asset_feature string, damage_category string, location object with side, lane_position, and "
  "relative_distance, severity string, confidence number 0 to 1, description string, "
  "recommended_action string. Use ASCII text. If no clear agency-actionable asset issue is visible, "
  "set found_issue=false, confidence<=0.34, and keep the description short."
)

INSPECTION_ELEMENTS = {
  "roadway": "Roadway",
  "roadside": "Roadside",
  "traffic services": "Traffic Services",
  "drainage": "Drainage",
  "vegetation/aesthetics": "Vegetation/Aesthetics",
  "vegetation / aesthetics": "Vegetation/Aesthetics",
}
SEVERITY_ALIASES = {
  "low": "minor",
  "minor": "minor",
  "medium": "moderate",
  "moderate": "moderate",
  "high": "severe",
  "severe": "severe",
  "unknown": "unknown",
}
LOCATION_SIDE_ALIASES = {
  "left": "left",
  "left side": "left",
  "center": "center",
  "centre": "center",
  "middle": "center",
  "right": "right",
  "right side": "right",
  "unknown": "unknown",
}
RELATIVE_DISTANCE_ALIASES = {
  "near": "near field",
  "near field": "near field",
  "close": "near field",
  "mid": "mid field",
  "middle": "mid field",
  "mid field": "mid field",
  "far": "far field",
  "far field": "far field",
  "distant": "far field",
  "unknown": "unknown",
}


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


def _clean_text(value: object, max_len: int, default: str = "") -> str:
  if value is None:
    return default
  text = " ".join(str(value).replace("\n", " ").split())
  return text[:max_len] if text else default


def _canonical_lookup(value: object, choices: dict[str, str], default: str) -> str:
  text = _clean_text(value, 80).lower()
  return choices.get(text, default)


def _bool_value(value: object) -> bool:
  if isinstance(value, bool):
    return value
  return _clean_text(value, 20).lower() in {"1", "true", "yes", "issue", "found"}


def _confidence_value(value: object) -> float:
  try:
    confidence = float(value)
  except (TypeError, ValueError):
    return 0.0
  if confidence > 1.0:
    confidence /= 100.0
  return max(0.0, min(confidence, 1.0))


def _extract_json_object(content: str) -> dict[str, object] | None:
  try:
    data = json.loads(content)
  except json.JSONDecodeError:
    start = content.find("{")
    end = content.rfind("}")
    if start < 0 or end <= start:
      return None
    try:
      data = json.loads(content[start:end + 1])
    except json.JSONDecodeError:
      return None

  return data if isinstance(data, dict) else None


def _driver_display(record: dict[str, object]) -> str:
  location = record.get("location") if isinstance(record.get("location"), dict) else {}
  lane_position = _clean_text(location.get("lane_position") if isinstance(location, dict) else "", 32)
  side = _clean_text(location.get("side") if isinstance(location, dict) else "", 16)
  location_text = lane_position if lane_position and lane_position != "unknown" else side

  pieces = [_clean_text(record.get("damage_category"), 28, "Road issue")]
  if location_text and location_text != "unknown":
    pieces.append(location_text)
  severity = _clean_text(record.get("severity"), 16)
  if severity and severity != "unknown":
    pieces.append(severity)
  return _clean_text(" - ".join(pieces), DRIVER_DISPLAY_MAX_CHARS, "Road issue")


def _empty_inspection(description: str = "") -> dict[str, object]:
  return {
    "schema_version": ROAD_INSPECTION_SCHEMA_VERSION,
    "source": "llm_agent",
    "timestamp": time.time(),
    "found_issue": False,
    "inspection_element": "Roadway",
    "asset_feature": "unknown",
    "damage_category": "None",
    "location": {
      "side": "unknown",
      "lane_position": "unknown",
      "relative_distance": "unknown",
    },
    "severity": "unknown",
    "confidence": 0.0,
    "description": description,
    "recommended_action": "none",
    "driver_display": "",
    "capture": {},
  }


def _normalize_inspection_response(content: str, full_payload: tuple[str, int, str, tuple[int, int]],
                                   crop_payload: tuple[str, int, str, tuple[int, int]]) -> tuple[bool, dict[str, object] | str]:
  data = _extract_json_object(content)
  if data is None:
    return False, "invalid json"

  found_issue = _bool_value(data.get("found_issue"))
  confidence = _confidence_value(data.get("confidence"))
  if confidence < MIN_DISPLAY_CONFIDENCE:
    found_issue = False

  location_src = data.get("location") if isinstance(data.get("location"), dict) else {}
  location = {
    "side": _canonical_lookup(location_src.get("side") if isinstance(location_src, dict) else None, LOCATION_SIDE_ALIASES, "unknown"),
    "lane_position": _clean_text(location_src.get("lane_position") if isinstance(location_src, dict) else None, 48, "unknown"),
    "relative_distance": _canonical_lookup(
      location_src.get("relative_distance") if isinstance(location_src, dict) else None,
      RELATIVE_DISTANCE_ALIASES,
      "unknown",
    ),
  }

  _, full_size, full_path, full_dims = full_payload
  _, crop_size, crop_path, crop_dims = crop_payload
  record: dict[str, object] = {
    "schema_version": ROAD_INSPECTION_SCHEMA_VERSION,
    "source": "llm_agent",
    "timestamp": time.time(),
    "found_issue": found_issue,
    "inspection_element": _canonical_lookup(data.get("inspection_element"), INSPECTION_ELEMENTS, "Roadway"),
    "asset_feature": _clean_text(data.get("asset_feature"), 64, "unknown"),
    "damage_category": _clean_text(data.get("damage_category"), 40, "Road issue" if found_issue else "None"),
    "location": location,
    "severity": _canonical_lookup(data.get("severity"), SEVERITY_ALIASES, "unknown"),
    "confidence": confidence,
    "description": _clean_text(data.get("description"), 160),
    "recommended_action": _clean_text(data.get("recommended_action"), 80, "human verification" if found_issue else "none"),
    "driver_display": "",
    "capture": {
      "full_path": full_path,
      "full_image_size_bytes": full_size,
      "full_dimensions": list(full_dims),
      "crop_path": crop_path,
      "crop_image_size_bytes": crop_size,
      "crop_dimensions": list(crop_dims),
    },
  }
  record["driver_display"] = _driver_display(record) if found_issue else ""
  return True, record


def _update_road_inspection(params: Params, inspection: dict[str, object] | None, active_inspection: dict[str, object] | None,
                            inspection_expiry: float, now: float) -> tuple[dict[str, object] | None, float]:
  if inspection and inspection.get("found_issue"):
    params.put(ROAD_INSPECTION_PARAM, inspection)
    return inspection, now + ADVISORY_HOLD_S

  if active_inspection and now < inspection_expiry:
    params.put(ROAD_INSPECTION_PARAM, active_inspection)
    return active_inspection, inspection_expiry

  params.put(ROAD_INSPECTION_PARAM, inspection or _empty_inspection())
  return None, 0.0


def _openai_vision_inspect(api_key: str, image_payloads: tuple[tuple[str, int, str, tuple[int, int]], ...]) -> tuple[bool, dict[str, object] | str]:
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
    "response_format": {"type": "json_object"},
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
  if not content:
    finish_reason = choice.get("finish_reason", "unknown")
    return False, f"empty response ({finish_reason})"
  if len(image_payloads) < 2:
    return False, "missing image payload"
  return _normalize_inspection_response(str(content).strip(), image_payloads[0], image_payloads[1])


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
  params.put(ROAD_INSPECTION_PARAM, _empty_inspection("not inspected yet"))
  sm = messaging.SubMaster(['deviceState'])
  sm_audio = messaging.SubMaster(['rawAudioData'])
  cloudlog.info("llm-agent: starting")
  rk = Ratekeeper(1.0)
  last_heartbeat = 0.0
  last_ping = 0.0
  warned_no_key = False
  active_inspection: dict[str, object] | None = None
  inspection_expiry = 0.0

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
            active_inspection, inspection_expiry = _update_road_inspection(
              params, None, active_inspection, inspection_expiry, now
            )
            cloudlog.warning("llm-agent: no front camera frame available from camerad")
            _log_local("no front camera frame available from camerad")
            _log_local("road inspection: held" if active_inspection else "road inspection: cleared")
          else:
            full_payload, crop_payload = image_payloads
            _, full_size, full_path, full_dims = full_payload
            _, crop_size, crop_path, crop_dims = crop_payload
            cloudlog.info(f"llm-agent: encoded frames full={full_size}B crop={crop_size}B")
            _log_local(f"encoded frame size={full_size}B dims={full_dims[0]}x{full_dims[1]} capture={full_path}")
            _log_local(f"encoded road crop size={crop_size}B dims={crop_dims[0]}x{crop_dims[1]} capture={crop_path}")
            request_start = time.monotonic()
            ok, detail = _openai_vision_inspect(api_key, image_payloads)
            request_elapsed = time.monotonic() - request_start
            response_now = time.monotonic()
            _log_local(f"vision response model={OPENAI_MODEL} elapsed={request_elapsed:.2f}s ok={ok}")
            if ok:
              assert isinstance(detail, dict)
              active_inspection, inspection_expiry = _update_road_inspection(
                params, detail, active_inspection, inspection_expiry, response_now
              )
              params.put(ADVISORY_PARAM, "")
              if detail.get("found_issue"):
                cloudlog.info(f"llm-agent: road issue: {detail.get('driver_display')}")
              else:
                cloudlog.info("llm-agent: no road maintenance issue found")
              _log_local(f"road inspection: {json.dumps(detail, sort_keys=True)}")
            else:
              active_inspection, inspection_expiry = _update_road_inspection(
                params, None, active_inspection, inspection_expiry, response_now
              )
              cloudlog.warning(f"llm-agent: OpenAI vision failed ({detail})")
              _log_local(f"OpenAI vision failed ({detail})")
              _log_local("road inspection: held" if active_inspection else "road inspection: cleared")
        except Exception as e:
          active_inspection, inspection_expiry = _update_road_inspection(
            params, None, active_inspection, inspection_expiry, now
          )
          cloudlog.warning(f"llm-agent: OpenAI request error: {e}")
          _log_local(f"OpenAI request error: {e}")
          _log_local("road inspection: held" if active_inspection else "road inspection: cleared")
      last_ping = now

    rk.keep_time()


if __name__ == "__main__":
  main()
