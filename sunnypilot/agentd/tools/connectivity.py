"""Connectivity Testing Tool — Reports network status and optionally
tests cloud API reachability.

Uses deviceState data from the SubMaster (network type, strength, carrier,
metered status) and can perform a lightweight HEAD request to the OpenAI
API endpoint to verify end-to-end cloud connectivity.

No API keys required for the basic status check. The optional ping test
uses only a HEAD request and does not consume API tokens.
"""

import time

import requests

from openpilot.common.swaglog import cloudlog
from openpilot.sunnypilot.agentd.tools.base_tool import BaseTool, Advisory
from openpilot.sunnypilot.agentd.tools.registry import register_tool

# --- Constants ---

PING_TIMEOUT_S = 3
PING_URL = "https://api.openai.com/v1/chat/completions"
PING_CACHE_TTL_S = 30  # Don't ping more than once every 30 seconds
STALE_PING_THRESHOLD_S = 120  # Athena ping older than 2 min = stale

# Network quality classification by network type
NETWORK_TYPE_QUALITY = {
  "none": "disconnected",
  "cell2G": "very_slow",
  "cell3G": "slow",
  "cell4G": "good",
  "cell5G": "excellent",
  "wifi": "good",
  "ethernet": "excellent",
}

STRENGTH_QUALITY = {
  "unknown": "unknown",
  "poor": "poor",
  "moderate": "moderate",
  "good": "good",
  "great": "excellent",
}


class ConnectivityChecker:
  """Cached endpoint reachability checker.

  Performs lightweight HEAD requests to the OpenAI API to test
  DNS resolution, TCP connection, and TLS handshake without consuming
  any API tokens. Results are cached for PING_CACHE_TTL_S seconds.
  """

  def __init__(self):
    self._last_ping_time: float = 0.0
    self._last_ping_ok: bool | None = None
    self._last_ping_latency_ms: float = 0.0

  def ping_api(self) -> dict:
    """Perform a lightweight HEAD request to the OpenAI API.

    Returns cached result if pinged recently (within PING_CACHE_TTL_S).
    Any HTTP response (even 401/405) means the endpoint is reachable.
    """
    now = time.monotonic()
    if self._last_ping_ok is not None and (now - self._last_ping_time) < PING_CACHE_TTL_S:
      return {
        "reachable": self._last_ping_ok,
        "latency_ms": self._last_ping_latency_ms,
        "cached": True,
      }

    start = time.monotonic()
    try:
      requests.head(PING_URL, timeout=PING_TIMEOUT_S)
      # Any HTTP response (even 405 Method Not Allowed) confirms reachability
      latency_ms = (time.monotonic() - start) * 1000
      self._last_ping_ok = True
      self._last_ping_latency_ms = round(latency_ms, 0)
      self._last_ping_time = now
      cloudlog.debug(f"agentd connectivity: API ping OK, {latency_ms:.0f}ms")
      return {"reachable": True, "latency_ms": self._last_ping_latency_ms, "cached": False}

    except requests.Timeout:
      latency_ms = (time.monotonic() - start) * 1000
      self._last_ping_ok = False
      self._last_ping_latency_ms = round(latency_ms, 0)
      self._last_ping_time = now
      cloudlog.debug("agentd connectivity: API ping timeout")
      return {"reachable": False, "latency_ms": self._last_ping_latency_ms, "cached": False,
              "error": "timeout"}

    except requests.RequestException as e:
      latency_ms = (time.monotonic() - start) * 1000
      self._last_ping_ok = False
      self._last_ping_latency_ms = round(latency_ms, 0)
      self._last_ping_time = now
      cloudlog.debug(f"agentd connectivity: API ping error: {e}")
      return {"reachable": False, "latency_ms": self._last_ping_latency_ms, "cached": False,
              "error": str(e)[:100]}


# Module-level singleton (shared across tool invocations, same pattern as FL511Client)
_checker = ConnectivityChecker()


@register_tool(
  name="check_connectivity",
  description="Check the device's current network connectivity status and optionally "
              "test reachability of the cloud AI API endpoint. Use this before heavy "
              "API operations when network quality is uncertain, or when you notice "
              "slow response times. Returns network type, signal strength, carrier info, "
              "metered status, and optional ping test results."
)
class ConnectivityTool(BaseTool):

  @classmethod
  def schema(cls) -> dict:
    return {
      "name": "check_connectivity",
      "description": cls.tool_description,
      "input_schema": {
        "type": "object",
        "properties": {
          "action": {
            "type": "string",
            "enum": ["status", "ping"],
            "description": (
              "'status': report current network type, strength, carrier, and metered "
              "status from device telemetry (fast, no network call). "
              "'ping': additionally perform a lightweight HEAD request to the cloud "
              "API to verify end-to-end reachability and measure latency."
            ),
          },
        },
        "required": ["action"],
      },
    }

  def execute(self, params: dict, context: dict) -> Advisory:
    action = params.get("action", "status")
    net = context.get("network", {})

    net_type = net.get("network_type", "none")
    net_strength = net.get("network_strength", "unknown")
    metered = net.get("network_metered", False)
    operator = net.get("operator", "")
    technology = net.get("technology", "")
    last_ping_ago = net.get("last_athena_ping_s_ago")

    # Classify network quality
    type_quality = NETWORK_TYPE_QUALITY.get(net_type, "unknown")
    strength_quality = STRENGTH_QUALITY.get(net_strength, "unknown")

    # Build status summary for the reason field
    reason_parts = [
      f"Network type: {net_type}",
      f"Signal strength: {net_strength}",
      f"Type quality: {type_quality}",
      f"Strength quality: {strength_quality}",
    ]

    if operator:
      reason_parts.append(f"Carrier: {operator}")
    if technology:
      reason_parts.append(f"Technology: {technology}")
    reason_parts.append(f"Metered connection: {'yes' if metered else 'no'}")

    if last_ping_ago is not None:
      reason_parts.append(f"Last Athena ping: {last_ping_ago:.0f}s ago")
      athena_stale = last_ping_ago > STALE_PING_THRESHOLD_S
    else:
      reason_parts.append("Last Athena ping: unknown")
      athena_stale = True

    # Determine alert severity based on network state
    if net_type == "none":
      alert_severity = 1  # warning
      alert_text = "No network connection"
    elif net_strength == "poor" or net_type == "cell2G":
      alert_severity = 1  # warning
      alert_text = f"Weak network: {net_type} {net_strength}"
    else:
      alert_severity = 0  # info
      alert_text = f"Network: {net_type} ({net_strength})"

    # Optional ping test
    if action == "ping":
      ping_result = _checker.ping_api()
      reason_parts.append("")
      reason_parts.append("API endpoint ping test:")
      reason_parts.append(f"  Reachable: {'yes' if ping_result['reachable'] else 'no'}")
      reason_parts.append(f"  Latency: {ping_result['latency_ms']:.0f}ms")
      if ping_result.get("cached"):
        reason_parts.append("  (cached result)")
      if ping_result.get("error"):
        reason_parts.append(f"  Error: {ping_result['error']}")

      if not ping_result["reachable"]:
        alert_severity = max(alert_severity, 1)  # warning
        alert_text = "Cloud API unreachable"
      elif ping_result["latency_ms"] > 2000:
        alert_text = f"Cloud API slow ({ping_result['latency_ms']:.0f}ms)"

    return Advisory(
      alert_active=True,
      alert_text=alert_text[:50],
      alert_severity=alert_severity,
      tool_name="check_connectivity",
      reason="\n".join(reason_parts),
    )
