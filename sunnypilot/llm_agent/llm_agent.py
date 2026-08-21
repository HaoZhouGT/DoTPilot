#!/usr/bin/env python3
import base64
import io
import json
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
ROAD_INSPECTION_PARAM = "LLMRoadInspection"
ROAD_INSPECTION_SCHEMA_VERSION = 1
DRIVER_DISPLAY_MAX_CHARS = 72
MIN_DISPLAY_CONFIDENCE = 0.35

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


def _clean_text(value: object, max_len: int, default: str = "") -> str:
  if value is None:
    return default
  text = " ".join(str(value).replace("\n", " ").split())
  return text[:max_len] if text else default


def _canonical_lookup(value: object, choices: dict[str, str], default: str) -> str:
  text = _clean_text(value, 80).lower()
  return choices.get(text, default)


def _canonical_alias(value: object, choices: dict[str, str], default: str) -> str:
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


def _normalize_inspection_response(content: str, image_size: int, capture_path: str) -> tuple[bool, dict[str, object] | str]:
  data = _extract_json_object(content)
  if data is None:
    return False, "invalid json"

  found_issue = _bool_value(data.get("found_issue"))
  confidence = _confidence_value(data.get("confidence"))
  if confidence < MIN_DISPLAY_CONFIDENCE:
    found_issue = False

  location_src = data.get("location") if isinstance(data.get("location"), dict) else {}
  location = {
    "side": _canonical_alias(location_src.get("side") if isinstance(location_src, dict) else None, LOCATION_SIDE_ALIASES, "unknown"),
    "lane_position": _clean_text(location_src.get("lane_position") if isinstance(location_src, dict) else None, 48, "unknown"),
    "relative_distance": _canonical_alias(
      location_src.get("relative_distance") if isinstance(location_src, dict) else None,
      RELATIVE_DISTANCE_ALIASES,
      "unknown",
    ),
  }

  record: dict[str, object] = {
    "schema_version": ROAD_INSPECTION_SCHEMA_VERSION,
    "source": "llm_agent",
    "timestamp": time.time(),
    "found_issue": found_issue,
    "inspection_element": _canonical_lookup(data.get("inspection_element"), INSPECTION_ELEMENTS, "Roadway"),
    "asset_feature": _clean_text(data.get("asset_feature"), 64, "unknown"),
    "damage_category": _clean_text(data.get("damage_category"), 40, "Road issue" if found_issue else "None"),
    "location": location,
    "severity": _canonical_alias(data.get("severity"), SEVERITY_ALIASES, "unknown"),
    "confidence": confidence,
    "description": _clean_text(data.get("description"), 160),
    "recommended_action": _clean_text(data.get("recommended_action"), 80, "human verification" if found_issue else "none"),
    "capture": {
      "path": capture_path,
      "image_size_bytes": image_size,
    },
  }
  record["driver_display"] = _driver_display(record) if found_issue else ""
  return True, record


def _openai_vision_inspect(api_key: str, image_b64: str, image_size: int, capture_path: str) -> tuple[bool, dict[str, object] | str]:
  headers = {
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json",
  }
  payload = {
    "model": OPENAI_MODEL,
    "messages": [
      {
        "role": "system",
        "content": (
          "You are a road maintenance inspection assistant. Return JSON only. "
          "Use cautious visual observations and do not invent measurements."
        ),
      },
      {
        "role": "user",
        "content": [
          {
            "type": "text",
            "text": (
              "Inspect this forward road image for visible maintenance or road-asset issues. "
              "Use an FDOT Maintenance Rating Program inspired taxonomy: inspection_element must be one of "
              "Roadway, Roadside, Traffic Services, Drainage, Vegetation/Aesthetics. "
              "Prefer roadway categories such as pothole, edge raveling, shoving, depression/bump, "
              "paved shoulder/turnout, joint/cracking, rutting, pavement marking, roadway sweeping/debris, "
              "sign/object marker, guardrail/attenuator, inlet/ditch/drain, or vegetation obstruction. "
              "Return JSON with exactly these top-level fields: found_issue boolean, inspection_element string, "
              "asset_feature string, damage_category string, location object with side, lane_position, and "
              "relative_distance, severity string, confidence number 0 to 1, description string, "
              "recommended_action string. Use ASCII text. If no clear asset issue is visible, set "
              "found_issue=false, confidence<=0.34, and keep the description short."
            )
          },
          {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
        ],
      },
    ],
    "max_tokens": 260,
    "temperature": 0,
    "response_format": {"type": "json_object"},
  }

  r = requests.post(OPENAI_CHAT_URL, headers=headers, json=payload, timeout=OPENAI_TIMEOUT_S)
  if r.status_code < 200 or r.status_code >= 300:
    return False, f"http {r.status_code}"

  body = r.json()
  content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
  return _normalize_inspection_response(str(content).strip(), image_size, capture_path)


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
            ok, detail = _openai_vision_inspect(api_key, image_b64, image_size, capture_path)
            if ok:
              assert isinstance(detail, dict)
              params.put_nonblocking(ROAD_INSPECTION_PARAM, detail)
              if detail.get("found_issue"):
                cloudlog.info(f"llm-agent: road issue: {detail.get('driver_display')}")
              else:
                cloudlog.info("llm-agent: no road maintenance issue found")
              _log_local(f"road inspection: {json.dumps(detail, sort_keys=True)}")
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
