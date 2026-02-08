"""FL511 Traffic Tool - Pulls real-time traffic events from the Florida 511 API.

Provides the AI agent with live traffic intelligence including accidents,
road closures, construction, and congestion data for Florida roadways.

API docs: https://fl511.com/developers/help
Rate limit: 10 calls per 60 seconds.
"""

import json
import math
import time
from typing import Any

import requests

from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from openpilot.sunnypilot.agentd.tools.base_tool import BaseTool, Advisory
from openpilot.sunnypilot.agentd.tools.registry import register_tool

FL511_API_URL = "https://fl511.com/api/getevents"
API_TIMEOUT_S = 5
CACHE_TTL_S = 30  # Cache results for 30 seconds (rate limit: 10 calls/60s)
DEFAULT_RADIUS_MI = 10  # Default search radius in miles
EARTH_RADIUS_MI = 3958.8  # Earth's radius in miles

# Severity mapping from FL511 to confidence levels for advisories
SEVERITY_MAP = {
  "Critical": {"confidence": 0.9, "alert_severity": 2, "speed_reduction_pct": 0.5},
  "Major": {"confidence": 0.8, "alert_severity": 1, "speed_reduction_pct": 0.35},
  "Moderate": {"confidence": 0.7, "alert_severity": 1, "speed_reduction_pct": 0.25},
  "Minor": {"confidence": 0.6, "alert_severity": 0, "speed_reduction_pct": 0.15},
  "Unknown": {"confidence": 0.5, "alert_severity": 0, "speed_reduction_pct": 0.1},
}

# Event types that should trigger speed reductions
SLOWDOWN_EVENT_TYPES = {
  "accidentsAndIncidents",
  "roadwork",
  "closures",
  "winterDrivingIndex",
}


def _haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
  """Calculate distance in miles between two lat/lon points."""
  lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
  dlat = lat2 - lat1
  dlon = lon2 - lon1
  a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
  return EARTH_RADIUS_MI * 2 * math.asin(math.sqrt(a))


class FL511Client:
  """Cached client for the FL511 traffic events API."""

  def __init__(self):
    self._cache: list[dict] | None = None
    self._cache_time: float = 0.0

  def get_events(self, api_key: str) -> list[dict]:
    """Fetch all traffic events from FL511, with caching."""
    now = time.monotonic()
    if self._cache is not None and (now - self._cache_time) < CACHE_TTL_S:
      return self._cache

    try:
      response = requests.get(
        FL511_API_URL,
        params={"key": api_key, "format": "json"},
        timeout=API_TIMEOUT_S,
      )
      response.raise_for_status()
      events = response.json()

      if isinstance(events, list):
        self._cache = events
        self._cache_time = now
        cloudlog.debug(f"agentd fl511: fetched {len(events)} events")
        return events
      else:
        cloudlog.warning(f"agentd fl511: unexpected response type: {type(events)}")
        return []

    except requests.Timeout:
      cloudlog.warning("agentd fl511: API timeout")
      return self._cache or []
    except requests.RequestException as e:
      cloudlog.warning(f"agentd fl511: API error: {e}")
      return self._cache or []
    except (json.JSONDecodeError, ValueError) as e:
      cloudlog.warning(f"agentd fl511: JSON parse error: {e}")
      return self._cache or []

  def get_nearby_events(self, api_key: str, lat: float, lon: float,
                        radius_mi: float = DEFAULT_RADIUS_MI,
                        direction: str | None = None,
                        roadway: str | None = None) -> list[dict]:
    """Get events near a GPS coordinate, optionally filtered by direction and roadway."""
    events = self.get_events(api_key)
    nearby = []

    for event in events:
      try:
        evt_lat = float(event.get("Latitude", 0))
        evt_lon = float(event.get("Longitude", 0))
      except (TypeError, ValueError):
        continue

      if evt_lat == 0 and evt_lon == 0:
        continue

      dist = _haversine_distance(lat, lon, evt_lat, evt_lon)
      if dist > radius_mi:
        continue

      # Optional filters
      if roadway and roadway.lower() not in event.get("RoadwayName", "").lower():
        continue
      if direction and direction.lower() not in event.get("DirectionOfTravel", "").lower():
        continue

      event["_distance_mi"] = round(dist, 1)
      nearby.append(event)

    # Sort by distance
    nearby.sort(key=lambda e: e.get("_distance_mi", 999))
    return nearby


# Module-level singleton client (shared across tool invocations)
_fl511_client = FL511Client()


@register_tool(
  name="get_traffic_ahead",
  description="Query real-time traffic conditions from the Florida 511 system. "
              "Returns nearby traffic events including accidents, road closures, "
              "construction zones, and congestion. Use this when you want to check "
              "for traffic incidents ahead on the current route, or when the driver "
              "is approaching an area that might have traffic issues. The tool uses "
              "the vehicle's current GPS location to find relevant events."
)
class FL511TrafficTool(BaseTool):

  @classmethod
  def schema(cls) -> dict:
    return {
      "name": "get_traffic_ahead",
      "description": cls.tool_description,
      "input_schema": {
        "type": "object",
        "properties": {
          "radius_miles": {
            "type": "number",
            "description": "Search radius in miles from current location. Default is 10.",
          },
          "roadway_filter": {
            "type": "string",
            "description": "Optional: filter events to a specific roadway (e.g., 'I-95', 'I-4', 'Florida Turnpike').",
          },
          "direction_filter": {
            "type": "string",
            "description": "Optional: filter by travel direction (e.g., 'Northbound', 'Southbound').",
          },
          "action": {
            "type": "string",
            "enum": ["check", "advise"],
            "description": "'check' returns a summary of nearby events for your analysis. "
                           "'advise' automatically generates speed/alert advisories based on the most severe nearby event.",
          },
        },
        "required": ["action"],
      },
    }

  def execute(self, params: dict, context: dict) -> Advisory:
    # Get API key
    api_key = Params().get("FL511ApiKey")
    if not api_key:
      cloudlog.warning("agentd fl511: no FL511ApiKey param set")
      return Advisory(
        alert_active=True,
        alert_text="FL511 API key not configured",
        alert_severity=0,
        tool_name="get_traffic_ahead",
        reason="missing_api_key",
      )
    api_key = api_key.decode('utf-8').strip()

    # Get vehicle GPS location from context
    vehicle = context.get("vehicle", {})
    map_data = context.get("map", {})
    gps = context.get("gps", {})

    lat = gps.get("latitude", 0.0)
    lon = gps.get("longitude", 0.0)

    if lat == 0 and lon == 0:
      return Advisory(
        alert_active=True,
        alert_text="No GPS location available",
        alert_severity=0,
        tool_name="get_traffic_ahead",
        reason="no_gps",
      )

    # Query FL511
    radius = params.get("radius_miles", DEFAULT_RADIUS_MI)
    roadway = params.get("roadway_filter")
    direction = params.get("direction_filter")
    action = params.get("action", "check")

    events = _fl511_client.get_nearby_events(
      api_key=api_key,
      lat=lat, lon=lon,
      radius_mi=radius,
      direction=direction,
      roadway=roadway,
    )

    if not events:
      return Advisory(
        tool_name="get_traffic_ahead",
        reason="no_events_nearby",
      )

    if action == "check":
      return self._build_check_advisory(events)
    else:
      return self._build_advise_advisory(events, vehicle)

  def _build_check_advisory(self, events: list[dict]) -> Advisory:
    """Return a summary advisory for the LLM to reason about."""
    summary_parts = []
    for i, evt in enumerate(events[:5]):  # Top 5 closest events
      dist = evt.get("_distance_mi", "?")
      severity = evt.get("Severity", "Unknown")
      event_type = evt.get("EventType", "Unknown")
      road = evt.get("RoadwayName", "Unknown")
      direction = evt.get("DirectionOfTravel", "")
      desc = evt.get("Description", "")[:100]
      location = evt.get("Location", "")

      summary_parts.append(
        f"#{i + 1} [{severity}] {event_type} on {road} {direction} - "
        f"{dist}mi away - {location} - {desc}"
      )

    summary = "; ".join(summary_parts)
    count = len(events)

    return Advisory(
      alert_active=True,
      alert_text=f"{count} traffic event(s) nearby",
      alert_severity=0,  # info
      tool_name="get_traffic_ahead",
      reason=f"FL511: {summary}",
    )

  def _build_advise_advisory(self, events: list[dict], vehicle: dict) -> Advisory:
    """Automatically generate speed/alert advisories from the most severe nearby event."""
    # Find the most severe event
    severity_order = ["Critical", "Major", "Moderate", "Minor", "Unknown"]
    events_sorted = sorted(
      events,
      key=lambda e: severity_order.index(e.get("Severity", "Unknown"))
      if e.get("Severity", "Unknown") in severity_order else 99,
    )

    worst = events_sorted[0]
    severity_str = worst.get("Severity", "Unknown")
    severity_info = SEVERITY_MAP.get(severity_str, SEVERITY_MAP["Unknown"])
    event_type = worst.get("EventType", "")
    road = worst.get("RoadwayName", "Unknown road")
    dist_mi = worst.get("_distance_mi", 0)
    desc = worst.get("Description", "")[:80]
    location = worst.get("Location", "")

    # Build alert text
    type_label = {
      "accidentsAndIncidents": "Accident",
      "roadwork": "Road work",
      "closures": "Road closure",
      "specialEvents": "Special event",
      "winterDrivingIndex": "Weather hazard",
    }.get(event_type, "Traffic event")

    alert_text = f"{type_label} on {road} - {dist_mi}mi ahead"
    if len(alert_text) > 50:
      alert_text = f"{type_label} ahead - {dist_mi}mi"

    advisory = Advisory(
      alert_active=True,
      alert_text=alert_text[:50],
      alert_severity=severity_info["alert_severity"],
      tool_name="get_traffic_ahead",
      reason=f"FL511 {severity_str}: {type_label} on {road} at {location}. {desc}",
    )

    # Add speed advisory for slowdown-worthy events
    if event_type in SLOWDOWN_EVENT_TYPES:
      current_speed_mph = vehicle.get("speed_mph", 60)
      cruise_mph = vehicle.get("cruise_set_mph", 65)
      base_speed = max(current_speed_mph, cruise_mph)
      reduction = severity_info["speed_reduction_pct"]
      target_mph = base_speed * (1.0 - reduction)
      target_ms = target_mph * 0.44704

      advisory.speed_active = True
      advisory.speed_limit_ms = target_ms
      advisory.speed_source = f"fl511_{event_type}"
      advisory.speed_confidence = severity_info["confidence"]
      advisory.distance_ahead_m = dist_mi * 1609.34  # miles to meters

    return advisory
