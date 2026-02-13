#!/usr/bin/env python3
import time

from openpilot.common.params import Params
from openpilot.common.realtime import Ratekeeper
from openpilot.common.swaglog import cloudlog


def main():
  params = Params()
  cloudlog.info("llm-agent: starting")
  rk = Ratekeeper(1.0)
  last_heartbeat = 0.0

  while True:
    # Keep this daemon intentionally minimal for now; emit sparse heartbeat logs.
    now = time.monotonic()
    if now - last_heartbeat >= 60.0:
      enabled = params.get_bool("LLMAgentEnabled")
      cloudlog.info(f"llm-agent: alive (enabled={enabled})")
      last_heartbeat = now
    rk.keep_time()


if __name__ == "__main__":
  main()

