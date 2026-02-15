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

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_TRANSCRIBE_URL = "https://api.openai.com/v1/audio/transcriptions"
OPENAI_MODEL = "gpt-4o-mini"
OPENAI_AUDIO_MODEL = "gpt-4o-mini-transcribe"
OPENAI_TIMEOUT_S = 15
OPENAI_PING_INTERVAL_S = 10
OPENAI_AUDIO_TIMEOUT_S = 20
JPEG_MAX_SIZE = (960, 540)
JPEG_QUALITY = 70
LOCAL_LOG_PATH = "/data/llm-agent-test/llm_agent_runtime.log"
CAPTURE_DIR = "/data/llm-agent-test/captures"
ADVISORY_PARAM = "LLMAgentAdvisory"
AUDIO_ENABLED_PARAM = "LLMAgentAudioEnabled"
AUDIO_TRIGGER_PARAM = "LLMAgentAudioTrigger"
AUDIO_CAPTURE_SECONDS = 2.0
AUDIO_CAPTURE_TIMEOUT_S = 8.0


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


def _to_short_advisory(summary: str) -> str:
  text = (summary or "").strip().lower()
  if text.startswith("pay attention to"):
    text = text[len("pay attention to"):].strip(" :,-.")

  if any(k in text for k in ("pedestrian", "walker", "person crossing")):
    return "Pedestrian nearby"
  if any(k in text for k in ("cyclist", "bicycl", "bike rider")):
    return "Cyclist nearby"
  if any(k in text for k in ("pothole", "rough road", "uneven road", "broken pavement")):
    return "Pothole risk"
  if any(k in text for k in ("obstacle", "debris", "object in road")):
    return "Obstacle ahead"
  if any(k in text for k in ("low visibility", "reduced visibility", "dark", "dimly lit", "fog")):
    return "Low visibility"
  if "lane" in text and any(k in text for k in ("unclear", "not visible", "faded", "marking")):
    return "Lane unclear"
  if any(k in text for k in ("vehicle ahead", "lead vehicle", "close following")):
    return "Vehicle ahead"
  if any(k in text for k in ("not a drivable road scene", "lack of visible road", "no visible road", "limited road context")):
    return "Road context unclear"
  return "Road hazard"


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
  sm = messaging.SubMaster(['deviceState'])
  sm_audio = messaging.SubMaster(['rawAudioData'])
  cloudlog.info("llm-agent: starting")
  rk = Ratekeeper(1.0)
  last_heartbeat = 0.0
  last_ping = 0.0
  warned_no_key = False

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
              advisory = _to_short_advisory(detail)
              params.put(ADVISORY_PARAM, advisory)
              cloudlog.info(f"llm-agent: road summary: {detail}")
              _log_local(f"road summary: {detail}")
              _log_local(f"ui advisory: {advisory}")
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
