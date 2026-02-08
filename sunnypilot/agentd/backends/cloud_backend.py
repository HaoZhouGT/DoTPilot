import json
import time

import requests

from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from openpilot.sunnypilot.agentd.backends.base import BaseLLMBackend

DEFAULT_MODEL = "claude-sonnet-4-20250514"
DEFAULT_MAX_TOKENS = 1024
API_TIMEOUT_S = 10


class CloudBackend(BaseLLMBackend):
  """Cloud LLM backend using the Anthropic Messages API.

  Reads the API key from the 'AgentApiKey' param. Sends driving context
  as a structured user message with optional image (JPEG base64).
  """

  def __init__(self, params: Params):
    self.params = params
    self.model = DEFAULT_MODEL
    self.max_tokens = DEFAULT_MAX_TOKENS
    self.api_url = "https://api.anthropic.com/v1/messages"

  def _get_api_key(self) -> str | None:
    key = self.params.get("AgentApiKey")
    if key:
      return key.decode('utf-8').strip()
    return None

  def is_available(self) -> bool:
    return self._get_api_key() is not None

  def invoke(self, context: dict, tools: list[dict], system_prompt: str) -> dict:
    api_key = self._get_api_key()
    if not api_key:
      raise RuntimeError("No API key configured (set AgentApiKey param)")

    # Build the user message content blocks
    content = []

    # Add image if available
    frame_b64 = context.pop("frame_jpeg_b64", None)
    if frame_b64:
      content.append({
        "type": "image",
        "source": {
          "type": "base64",
          "media_type": "image/jpeg",
          "data": frame_b64,
        },
      })

    # Add structured driving context as text
    context_text = self._format_context(context)
    content.append({
      "type": "text",
      "text": context_text,
    })

    # Build API request
    payload = {
      "model": self.model,
      "max_tokens": self.max_tokens,
      "system": system_prompt,
      "tools": tools,
      "messages": [
        {"role": "user", "content": content},
      ],
    }

    headers = {
      "x-api-key": api_key,
      "anthropic-version": "2023-06-01",
      "content-type": "application/json",
    }

    start_time = time.monotonic()
    try:
      response = requests.post(
        self.api_url,
        headers=headers,
        json=payload,
        timeout=API_TIMEOUT_S,
      )
      response.raise_for_status()
      result = response.json()
    except requests.Timeout:
      cloudlog.warning("agentd: cloud API timeout")
      raise
    except requests.RequestException as e:
      cloudlog.warning(f"agentd: cloud API error: {e}")
      raise

    latency_ms = (time.monotonic() - start_time) * 1000
    cloudlog.debug(f"agentd: cloud inference latency: {latency_ms:.0f}ms")

    return result

  def _format_context(self, context: dict) -> str:
    """Format driving context as a readable string for the LLM."""
    lines = ["Current driving situation:"]

    v = context.get("vehicle", {})
    lines.append(f"\nVehicle: {v.get('speed_mph', 0):.0f} mph, "
                 f"cruise set to {v.get('cruise_set_mph', 0):.0f} mph, "
                 f"accel {v.get('acceleration_ms2', 0):.1f} m/s², "
                 f"steering {v.get('steering_angle_deg', 0):.0f}°")
    if v.get('brake_pressed'):
      lines.append("  Driver is pressing brake")
    if v.get('gas_pressed'):
      lines.append("  Driver is pressing gas")
    if v.get('left_blinker'):
      lines.append("  Left blinker is on")
    if v.get('right_blinker'):
      lines.append("  Right blinker is on")

    leads = context.get("leads", [])
    if leads:
      lines.append("\nDetected vehicles ahead:")
      for lead in leads:
        lines.append(f"  Lead {lead['index']}: {lead['distance_m']:.0f}m ahead, "
                     f"going {lead['speed_mph']:.0f} mph "
                     f"(relative: {lead['relative_speed_mph']:+.0f} mph)")
    else:
      lines.append("\nNo vehicles detected ahead by radar.")

    m = context.get("map", {})
    if m.get("road_name"):
      lines.append(f"\nRoad: {m['road_name']}")
    if m.get("speed_limit_mph"):
      lines.append(f"  Speed limit: {m['speed_limit_mph']:.0f} mph")
    if m.get("speed_limit_ahead_mph"):
      lines.append(f"  Speed limit ahead: {m['speed_limit_ahead_mph']:.0f} mph "
                   f"in {m['speed_limit_ahead_distance_m']:.0f}m")

    sys = context.get("system", {})
    if sys.get("openpilot_enabled"):
      lines.append("\nDoTPilot: engaged and active")
    else:
      lines.append("\nDoTPilot: not engaged")

    lines.append("\nAnalyze the driving situation from the camera image and data above. "
                 "If you detect any conditions that require action (hazards, construction, "
                 "slow vehicles, unusual conditions), use the appropriate tools. "
                 "If conditions are normal, respond with a brief status only.")

    return "\n".join(lines)
