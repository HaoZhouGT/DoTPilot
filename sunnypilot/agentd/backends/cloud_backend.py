import json
import time

import requests

from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from openpilot.sunnypilot.agentd.backends.base import BaseLLMBackend

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_MAX_TOKENS = 800
API_TIMEOUT_S = 8
COOLDOWN_S = 5


class CloudBackend(BaseLLMBackend):
  """Cloud LLM backend using the OpenAI Chat Completions API.

  Reads the API key from the 'AgentApiKey' param. Sends driving context
  as a structured user message with optional image (JPEG base64), and
  returns a normalized response in the existing internal content-block format.
  """

  def __init__(self, params: Params):
    self.params = params
    self.model = DEFAULT_MODEL
    self.max_tokens = DEFAULT_MAX_TOKENS
    self.api_url = "https://api.openai.com/v1/chat/completions"
    self._cooldown_until: float = 0.0

  def _get_api_key(self) -> str | None:
    key = self.params.get("AgentApiKey")
    if key:
      return key.decode('utf-8').strip()
    return None

  def is_available(self) -> bool:
    return self._get_api_key() is not None and time.monotonic() >= self._cooldown_until

  def invoke(self, context: dict, tools: list[dict], system_prompt: str) -> dict:
    if time.monotonic() < self._cooldown_until:
      raise RuntimeError("Cloud backend is cooling down after recent failures")

    api_key = self._get_api_key()
    if not api_key:
      raise RuntimeError("No API key configured (set AgentApiKey param)")

    # Build the user message content blocks
    content = []

    # Add image if available
    frame_b64 = context.get("frame_jpeg_b64")
    if frame_b64:
      content.append({
        "type": "image_url",
        "image_url": {"url": f"data:image/jpeg;base64,{frame_b64}"},
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
      "temperature": 0.2,
      "tools": self._to_openai_tools(tools),
      "tool_choice": "auto",
      "messages": [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content},
      ],
    }

    headers = {
      "Authorization": f"Bearer {api_key}",
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
      raw = response.json()
      result = self._normalize_response(raw)
      self._cooldown_until = 0.0
    except requests.Timeout:
      self._cooldown_until = time.monotonic() + COOLDOWN_S
      cloudlog.warning("agentd: cloud API timeout")
      raise
    except requests.RequestException as e:
      self._cooldown_until = time.monotonic() + COOLDOWN_S
      cloudlog.warning(f"agentd: cloud API error: {e}")
      raise
    except (ValueError, KeyError, TypeError) as e:
      self._cooldown_until = time.monotonic() + COOLDOWN_S
      cloudlog.warning(f"agentd: cloud API parse error: {e}")
      raise RuntimeError("Invalid cloud model response") from e

    latency_ms = (time.monotonic() - start_time) * 1000
    cloudlog.debug(f"agentd: cloud inference latency: {latency_ms:.0f}ms")

    return result

  def _to_openai_tools(self, tools: list[dict]) -> list[dict]:
    """Convert internal tool schemas into OpenAI function-calling format."""
    converted = []
    for tool in tools:
      converted.append({
        "type": "function",
        "function": {
          "name": tool.get("name", ""),
          "description": tool.get("description", ""),
          "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
        },
      })
    return converted

  def _normalize_response(self, raw: dict) -> dict:
    """Normalize OpenAI chat-completions output to internal content blocks."""
    choices = raw.get("choices", [])
    if not choices:
      return {"content": []}

    message = choices[0].get("message", {})
    content_blocks: list[dict] = []

    content = message.get("content")
    if isinstance(content, str) and content.strip():
      content_blocks.append({"type": "text", "text": content[:2000]})
    elif isinstance(content, list):
      text_parts = [
        item.get("text", "")
        for item in content
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
      ]
      text = "\n".join(part for part in text_parts if part.strip())
      if text:
        content_blocks.append({"type": "text", "text": text[:2000]})

    for call in message.get("tool_calls", []) or []:
      if call.get("type") != "function":
        continue
      fn = call.get("function", {})
      name = fn.get("name", "")
      raw_args = fn.get("arguments", "{}")
      try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else {}
      except ValueError:
        args = {}
      content_blocks.append({
        "type": "tool_use",
        "name": name,
        "input": args if isinstance(args, dict) else {},
      })

    return {"content": content_blocks}

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

    gps = context.get("gps", {})
    if gps.get("has_fix"):
      lines.append(f"\nGPS: {gps['latitude']:.6f}, {gps['longitude']:.6f} "
                   f"(bearing {gps.get('bearing_deg', 0):.0f}°, "
                   f"accuracy {gps.get('accuracy_m', 0):.0f}m)")
    else:
      lines.append("\nGPS: no fix")

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

    net = context.get("network", {})
    net_type = net.get("network_type", "none")
    net_strength = net.get("network_strength", "unknown")
    if net_type != "none":
      net_line = f"\nNetwork: {net_type}, strength {net_strength}"
      if net.get("operator"):
        net_line += f", carrier {net['operator']}"
      if net.get("network_metered"):
        net_line += " (metered)"
      lines.append(net_line)
      ping_ago = net.get("last_athena_ping_s_ago")
      if ping_ago is not None:
        if ping_ago > 120:
          lines.append(f"  Cloud connectivity: last successful ping {ping_ago:.0f}s ago (stale)")
        else:
          lines.append(f"  Cloud connectivity: last ping {ping_ago:.0f}s ago")
    else:
      lines.append("\nNetwork: disconnected (no network)")

    lines.append("\nAnalyze the driving situation from the camera image and data above. "
                 "If you detect any conditions that require action (hazards, construction, "
                 "slow vehicles, unusual conditions), use the appropriate tools. "
                 "If conditions are normal, respond with a brief status only.")

    return "\n".join(lines)
