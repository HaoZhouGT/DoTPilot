#!/usr/bin/env python3
import os
import time

import requests

from openpilot.common.params import Params
from openpilot.common.realtime import Ratekeeper
from openpilot.common.swaglog import cloudlog

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = "gpt-4o-mini"
OPENAI_TIMEOUT_S = 15
OPENAI_PING_INTERVAL_S = 300


def _read_api_key(params: Params) -> str:
  key = params.get("AgentApiKey", encoding="utf-8")
  if key:
    return key.strip()
  return os.getenv("OPENAI_API_KEY", "").strip()


def _openai_ping(api_key: str) -> tuple[bool, str]:
  headers = {
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json",
  }
  payload = {
    "model": OPENAI_MODEL,
    "messages": [
      {"role": "system", "content": "You are a concise driving assistant test endpoint."},
      {"role": "user", "content": "Reply with exactly: llm-agent-online"},
    ],
    "max_tokens": 16,
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
          ok, detail = _openai_ping(api_key)
          if ok:
            cloudlog.info(f"llm-agent: OpenAI ping ok ({detail})")
          else:
            cloudlog.warning(f"llm-agent: OpenAI ping failed ({detail})")
        except Exception as e:
          cloudlog.warning(f"llm-agent: OpenAI request error: {e}")
      last_ping = now

    rk.keep_time()


if __name__ == "__main__":
  main()
