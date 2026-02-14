#!/usr/bin/env python3
import base64
import io
import os
import time

import requests
from PIL import Image
from msgq.visionipc import VisionIpcClient, VisionStreamType

from openpilot.common.params import Params
from openpilot.common.realtime import Ratekeeper
from openpilot.common.swaglog import cloudlog
from openpilot.system.camerad.snapshot import extract_image

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = "gpt-4o-mini"
OPENAI_TIMEOUT_S = 15
OPENAI_PING_INTERVAL_S = 300


def _read_api_key(params: Params) -> str:
  key = params.get("AgentApiKey", encoding="utf-8")
  if key:
    return key.strip()
  return os.getenv("OPENAI_API_KEY", "").strip()


def _capture_front_camera_jpeg_b64() -> str | None:
  stream = VisionStreamType.VISION_STREAM_ROAD
  available = VisionIpcClient.available_streams("camerad", block=False)
  if stream not in available:
    if VisionStreamType.VISION_STREAM_WIDE_ROAD in available:
      stream = VisionStreamType.VISION_STREAM_WIDE_ROAD
    else:
      return None

  client = VisionIpcClient("camerad", stream, True)
  deadline = time.monotonic() + 2.0
  while time.monotonic() < deadline and not client.connect(False):
    time.sleep(0.05)

  if not client.is_connected() or not client.num_buffers:
    return None

  buf = client.recv()
  if buf is None:
    return None

  rgb = extract_image(buf)
  img = Image.fromarray(rgb)
  img.thumbnail((960, 540))

  with io.BytesIO() as out:
    img.save(out, format="JPEG", quality=70, optimize=True)
    return base64.b64encode(out.getvalue()).decode("utf-8")


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
        "content": "You describe road scenes for debugging. Be concise and factual.",
      },
      {
        "role": "user",
        "content": [
          {"type": "text", "text": "What do you see in this front road camera frame? One short sentence."},
          {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
        ],
      },
    ],
    "max_tokens": 80,
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
  cloudlog.info("llm-agent: starting")
  rk = Ratekeeper(1.0)
  last_heartbeat = 0.0
  last_ping = 0.0
  warned_no_key = False

  while True:
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
          image_b64 = _capture_front_camera_jpeg_b64()
          if not image_b64:
            cloudlog.warning("llm-agent: no front camera frame available from camerad")
          else:
            ok, detail = _openai_vision_describe(api_key, image_b64)
            if ok:
              cloudlog.info(f"llm-agent: vision says: {detail}")
            else:
              cloudlog.warning(f"llm-agent: OpenAI vision failed ({detail})")
        except Exception as e:
          cloudlog.warning(f"llm-agent: OpenAI request error: {e}")
      last_ping = now

    rk.keep_time()


if __name__ == "__main__":
  main()
