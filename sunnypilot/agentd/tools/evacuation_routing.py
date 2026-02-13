"""Evacuation Routing Tool — Multi-source evacuation intelligence for Florida.

Aggregates real-time data from:
  - NWS Weather Alerts (hurricane, flood, storm surge warnings)
  - FDEM Evacuation Zones (zone A–F classification)
  - FDEM Emergency Shelters (open shelters + pre-planned inventory)
  - FL511 Traffic Events (road closures on evacuation routes)
  - OSRM (open-source turn-by-turn route planning)

No API keys required for NWS, FDEM, or OSRM. FL511 reuses existing client.
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
from openpilot.sunnypilot.agentd.tools.fl511_traffic import _fl511_client, _haversine_distance

# --- Constants ---

API_TIMEOUT_S = 5
MI_TO_M = 1609.34
M_TO_MI = 0.000621371
MPH_TO_MS = 0.44704

# NWS
NWS_API_URL = "https://api.weather.gov/alerts/active"
NWS_CACHE_TTL_S = 60
NWS_USER_AGENT = "DoTPilot/1.0 (github.com/HaoZhouGT/DoTPilot)"

# Events that suggest evacuation may be needed
EVACUATION_EVENTS = {
  "Hurricane Warning",
  "Hurricane Watch",
  "Tropical Storm Warning",
  "Tropical Storm Watch",
  "Storm Surge Warning",
  "Storm Surge Watch",
  "Flood Warning",
  "Flash Flood Warning",
  "Tornado Warning",
  "Extreme Wind Warning",
  "Coastal Flood Warning",
  "Tsunami Warning",
}

# NWS severity → internal severity mapping
NWS_SEVERITY_MAP = {
  "Extreme": 2,   # critical
  "Severe": 2,    # critical
  "Moderate": 1,  # warning
  "Minor": 0,     # info
  "Unknown": 0,
}

# FDEM ArcGIS REST endpoints (org ID: 3wFbqsFPLeKqOlIK)
FDEM_BASE = "https://services.arcgis.com/3wFbqsFPLeKqOlIK/arcgis/rest/services"
FDEM_EVAC_ZONES_URL = f"{FDEM_BASE}/Evacuation_Zones_20230608/FeatureServer/12/query"
FDEM_EVAC_ROUTES_URL = f"{FDEM_BASE}/Evacuation_Routes_Hosted/FeatureServer/0/query"
FDEM_OPEN_SHELTERS_URL = f"{FDEM_BASE}/Open_Shelters_in_Florida_(View_Only)/FeatureServer/0/query"
FDEM_SHELTER_INVENTORY_URL = f"{FDEM_BASE}/Risk_Shelter_Inventory_General/FeatureServer/0/query"
FDEM_CACHE_TTL_S = 300  # 5 minutes for static GIS data

# OSRM public demo server
OSRM_API_URL = "https://router.project-osrm.org/route/v1/driving"
OSRM_CACHE_TTL_S = 60

# Evacuation zone priority (lower = more urgent)
ZONE_PRIORITY = {"A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6}


# --- API Clients ---

class NWSClient:
  """Cached client for NWS Weather Alerts API."""

  def __init__(self):
    self._cache: list[dict] | None = None
    self._cache_time: float = 0.0
    self._cache_key: str = ""

  def get_alerts(self, lat: float, lon: float) -> list[dict]:
    """Get active weather alerts near a GPS point."""
    cache_key = f"{lat:.2f},{lon:.2f}"
    now = time.monotonic()

    if (self._cache is not None and
        cache_key == self._cache_key and
        (now - self._cache_time) < NWS_CACHE_TTL_S):
      return self._cache

    try:
      response = requests.get(
        NWS_API_URL,
        params={"point": f"{lat:.4f},{lon:.4f}", "status": "actual"},
        headers={
          "User-Agent": NWS_USER_AGENT,
          "Accept": "application/geo+json",
        },
        timeout=API_TIMEOUT_S,
      )
      response.raise_for_status()
      data = response.json()

      alerts = []
      for feature in data.get("features", []):
        props = feature.get("properties", {})
        event = props.get("event", "")

        alerts.append({
          "event": event,
          "severity": props.get("severity", "Unknown"),
          "urgency": props.get("urgency", "Unknown"),
          "headline": props.get("headline", ""),
          "description": (props.get("description", "") or "")[:500],
          "instruction": (props.get("instruction", "") or "")[:500],
          "area": props.get("areaDesc", ""),
          "onset": props.get("onset", ""),
          "expires": props.get("expires", ""),
          "is_evacuation_relevant": event in EVACUATION_EVENTS,
        })

      self._cache = alerts
      self._cache_time = now
      self._cache_key = cache_key
      cloudlog.debug(f"agentd evac: NWS returned {len(alerts)} alerts")
      return alerts

    except requests.Timeout:
      cloudlog.warning("agentd evac: NWS API timeout")
      return self._cache or []
    except requests.RequestException as e:
      cloudlog.warning(f"agentd evac: NWS API error: {e}")
      return self._cache or []
    except (json.JSONDecodeError, ValueError, KeyError) as e:
      cloudlog.warning(f"agentd evac: NWS parse error: {e}")
      return self._cache or []


class FDEMClient:
  """Cached client for FDEM ArcGIS REST services."""

  def __init__(self):
    self._zone_cache: dict | None = None
    self._zone_cache_time: float = 0.0
    self._zone_cache_key: str = ""
    self._shelter_cache: list[dict] | None = None
    self._shelter_cache_time: float = 0.0
    self._shelter_cache_key: str = ""
    self._route_cache: list[dict] | None = None
    self._route_cache_time: float = 0.0
    self._route_cache_key: str = ""

  def get_evacuation_zone(self, lat: float, lon: float) -> dict | None:
    """Query FDEM for the evacuation zone at a given lat/lon."""
    cache_key = f"{lat:.4f},{lon:.4f}"
    now = time.monotonic()

    if (self._zone_cache is not None and
        cache_key == self._zone_cache_key and
        (now - self._zone_cache_time) < FDEM_CACHE_TTL_S):
      return self._zone_cache

    try:
      response = requests.get(
        FDEM_EVAC_ZONES_URL,
        params={
          "geometry": f"{lon},{lat}",
          "geometryType": "esriGeometryPoint",
          "spatialRel": "esriSpatialRelIntersects",
          "outFields": "EZone,County_Nam,STATUS",
          "returnGeometry": "false",
          "f": "json",
        },
        timeout=API_TIMEOUT_S,
      )
      response.raise_for_status()
      data = response.json()

      features = data.get("features", [])
      if not features:
        self._zone_cache = None
        self._zone_cache_time = now
        self._zone_cache_key = cache_key
        return None

      attrs = features[0].get("attributes", {})
      zone = {
        "zone": attrs.get("EZone", "Unknown"),
        "county": attrs.get("County_Nam", "Unknown"),
        "status": attrs.get("STATUS", "Unknown"),
        "priority": ZONE_PRIORITY.get(attrs.get("EZone", ""), 99),
      }

      self._zone_cache = zone
      self._zone_cache_time = now
      self._zone_cache_key = cache_key
      cloudlog.debug(f"agentd evac: FDEM zone={zone['zone']} county={zone['county']}")
      return zone

    except requests.Timeout:
      cloudlog.warning("agentd evac: FDEM zones API timeout")
      return self._zone_cache
    except requests.RequestException as e:
      cloudlog.warning(f"agentd evac: FDEM zones API error: {e}")
      return self._zone_cache
    except (json.JSONDecodeError, ValueError, KeyError) as e:
      cloudlog.warning(f"agentd evac: FDEM zones parse error: {e}")
      return self._zone_cache

  def get_nearby_shelters(self, lat: float, lon: float,
                          radius_mi: float = 25.0) -> list[dict]:
    """Find emergency shelters near a GPS point.

    Queries both open/active shelters and the pre-planned inventory.
    """
    cache_key = f"{lat:.3f},{lon:.3f},{radius_mi}"
    now = time.monotonic()

    if (self._shelter_cache is not None and
        cache_key == self._shelter_cache_key and
        (now - self._shelter_cache_time) < FDEM_CACHE_TTL_S):
      return self._shelter_cache

    shelters = []

    # Try open shelters first (populated during active events)
    open_shelters = self._query_shelters(
      FDEM_OPEN_SHELTERS_URL, lat, lon, radius_mi,
      fields="label,street,city,county,zip,gen_capacity,gen_occupancy,"
             "gen_availability,pet_friendly,special_needs,status",
      source="active",
    )
    shelters.extend(open_shelters)

    # Fall back to / supplement with shelter inventory
    if len(shelters) < 5:
      inventory = self._query_shelters(
        FDEM_SHELTER_INVENTORY_URL, lat, lon, radius_mi,
        fields="Name,Address,City,COUNTY,Zip,Risk_Capacity_Spaces,"
               "General_Pop,SPECIAL_NEEDS,Pet_Friendly,Evacuation_Zone",
        source="inventory",
      )
      shelters.extend(inventory)

    # Sort by distance
    shelters.sort(key=lambda s: s.get("distance_mi", 999))

    self._shelter_cache = shelters
    self._shelter_cache_time = now
    self._shelter_cache_key = cache_key
    cloudlog.debug(f"agentd evac: found {len(shelters)} shelters")
    return shelters

  def _query_shelters(self, url: str, lat: float, lon: float,
                      radius_mi: float, fields: str, source: str) -> list[dict]:
    """Query an ArcGIS shelter layer."""
    radius_m = radius_mi * MI_TO_M
    try:
      response = requests.get(
        url,
        params={
          "geometry": f"{lon},{lat}",
          "geometryType": "esriGeometryPoint",
          "spatialRel": "esriSpatialRelIntersects",
          "distance": str(radius_m),
          "units": "esriSRUnit_Meter",
          "outFields": fields,
          "returnGeometry": "true",
          "f": "json",
          "resultRecordCount": "20",
        },
        timeout=API_TIMEOUT_S,
      )
      response.raise_for_status()
      data = response.json()

      results = []
      for feature in data.get("features", []):
        attrs = feature.get("attributes", {})
        geom = feature.get("geometry", {})

        # Get shelter coordinates
        s_lon = geom.get("x", 0)
        s_lat = geom.get("y", 0)

        # Handle Web Mercator → approximate lat/lon if coords are large
        if abs(s_lon) > 180 or abs(s_lat) > 90:
          s_lon, s_lat = self._web_mercator_to_wgs84(s_lon, s_lat)

        dist = _haversine_distance(lat, lon, s_lat, s_lon) if s_lat != 0 else 999.0

        if source == "active":
          shelter = {
            "name": attrs.get("label", "Unknown Shelter"),
            "address": f"{attrs.get('street', '')} {attrs.get('city', '')} {attrs.get('zip', '')}".strip(),
            "county": attrs.get("county", ""),
            "capacity": attrs.get("gen_capacity", 0) or 0,
            "occupancy": attrs.get("gen_occupancy", 0) or 0,
            "availability": attrs.get("gen_availability", 0) or 0,
            "pet_friendly": attrs.get("pet_friendly", "No"),
            "special_needs": attrs.get("special_needs", "No"),
            "status": attrs.get("status", "Unknown"),
            "source": "active",
            "distance_mi": round(dist, 1),
            "latitude": round(s_lat, 6),
            "longitude": round(s_lon, 6),
          }
        else:
          shelter = {
            "name": attrs.get("Name", "Unknown Shelter"),
            "address": f"{attrs.get('Address', '')} {attrs.get('City', '')} {attrs.get('Zip', '')}".strip(),
            "county": attrs.get("COUNTY", ""),
            "capacity": attrs.get("Risk_Capacity_Spaces", 0) or 0,
            "pet_friendly": attrs.get("Pet_Friendly", "No"),
            "special_needs": attrs.get("SPECIAL_NEEDS", "No"),
            "evac_zone": attrs.get("Evacuation_Zone", ""),
            "source": "inventory",
            "distance_mi": round(dist, 1),
            "latitude": round(s_lat, 6),
            "longitude": round(s_lon, 6),
          }

        if dist <= radius_mi:
          results.append(shelter)

      return results

    except requests.Timeout:
      cloudlog.warning(f"agentd evac: FDEM shelters ({source}) timeout")
      return []
    except requests.RequestException as e:
      cloudlog.warning(f"agentd evac: FDEM shelters ({source}) error: {e}")
      return []
    except (json.JSONDecodeError, ValueError, KeyError) as e:
      cloudlog.warning(f"agentd evac: FDEM shelters ({source}) parse error: {e}")
      return []

  def get_nearby_routes(self, lat: float, lon: float,
                        radius_mi: float = 15.0) -> list[dict]:
    """Find designated evacuation routes near a GPS point."""
    cache_key = f"{lat:.3f},{lon:.3f},{radius_mi}"
    now = time.monotonic()

    if (self._route_cache is not None and
        cache_key == self._route_cache_key and
        (now - self._route_cache_time) < FDEM_CACHE_TTL_S):
      return self._route_cache

    radius_m = radius_mi * MI_TO_M
    try:
      response = requests.get(
        FDEM_EVAC_ROUTES_URL,
        params={
          "geometry": f"{lon},{lat}",
          "geometryType": "esriGeometryPoint",
          "spatialRel": "esriSpatialRelIntersects",
          "distance": str(radius_m),
          "units": "esriSRUnit_Meter",
          "outFields": "NAME,HWY_NUM,SHIELD,NAMELSAD",
          "returnGeometry": "false",
          "f": "json",
          "resultRecordCount": "10",
        },
        timeout=API_TIMEOUT_S,
      )
      response.raise_for_status()
      data = response.json()

      routes = []
      for feature in data.get("features", []):
        attrs = feature.get("attributes", {})
        name = attrs.get("NAME", "")
        hwy = attrs.get("HWY_NUM", "")
        shield = attrs.get("SHIELD", "")
        county = attrs.get("NAMELSAD", "")

        route_name = name or f"{shield}-{hwy}" if shield else hwy
        if route_name:
          routes.append({
            "name": route_name.strip(),
            "highway_number": hwy,
            "shield_type": shield,
            "county": county,
          })

      self._route_cache = routes
      self._route_cache_time = now
      self._route_cache_key = cache_key
      cloudlog.debug(f"agentd evac: found {len(routes)} evac routes")
      return routes

    except requests.Timeout:
      cloudlog.warning("agentd evac: FDEM routes API timeout")
      return self._route_cache or []
    except requests.RequestException as e:
      cloudlog.warning(f"agentd evac: FDEM routes API error: {e}")
      return self._route_cache or []
    except (json.JSONDecodeError, ValueError, KeyError) as e:
      cloudlog.warning(f"agentd evac: FDEM routes parse error: {e}")
      return self._route_cache or []

  @staticmethod
  def _web_mercator_to_wgs84(x: float, y: float) -> tuple[float, float]:
    """Approximate conversion from Web Mercator (3857) to WGS84 (4326)."""
    lon = x * 180.0 / 20037508.34
    lat = (math.atan(math.exp(y * math.pi / 20037508.34)) * 360.0 / math.pi) - 90.0
    return lon, lat


class OSRMClient:
  """Cached client for OSRM public routing API."""

  def __init__(self):
    self._cache: dict | None = None
    self._cache_time: float = 0.0
    self._cache_key: str = ""

  def get_route(self, origin_lat: float, origin_lon: float,
                dest_lat: float, dest_lon: float) -> dict | None:
    """Get driving route between two points."""
    cache_key = f"{origin_lat:.3f},{origin_lon:.3f}-{dest_lat:.3f},{dest_lon:.3f}"
    now = time.monotonic()

    if (self._cache is not None and
        cache_key == self._cache_key and
        (now - self._cache_time) < OSRM_CACHE_TTL_S):
      return self._cache

    coords = f"{origin_lon},{origin_lat};{dest_lon},{dest_lat}"
    try:
      response = requests.get(
        f"{OSRM_API_URL}/{coords}",
        params={
          "overview": "full",
          "steps": "true",
          "geometries": "geojson",
        },
        timeout=API_TIMEOUT_S,
      )
      response.raise_for_status()
      data = response.json()

      if data.get("code") != "Ok" or not data.get("routes"):
        cloudlog.warning(f"agentd evac: OSRM no route: {data.get('code')}")
        return None

      route = data["routes"][0]
      legs = route.get("legs", [])

      # Build step-by-step directions
      steps = []
      for leg in legs:
        for step in leg.get("steps", []):
          name = step.get("name", "")
          maneuver = step.get("maneuver", {})
          modifier = maneuver.get("modifier", "")
          step_type = maneuver.get("type", "")
          dist_m = step.get("distance", 0)
          duration_s = step.get("duration", 0)

          if step_type == "depart":
            instruction = f"Head {modifier} on {name}" if name else f"Depart {modifier}"
          elif step_type == "arrive":
            instruction = "Arrive at destination"
          elif step_type == "turn":
            instruction = f"Turn {modifier} onto {name}" if name else f"Turn {modifier}"
          elif step_type == "merge":
            instruction = f"Merge onto {name}" if name else f"Merge {modifier}"
          elif step_type in ("on ramp", "off ramp"):
            instruction = f"Take ramp onto {name}" if name else f"Take {modifier} ramp"
          elif step_type == "fork":
            instruction = f"Keep {modifier} onto {name}" if name else f"Keep {modifier}"
          elif step_type == "continue":
            instruction = f"Continue on {name}" if name else "Continue straight"
          elif step_type == "roundabout":
            instruction = f"Take roundabout to {name}" if name else "Take roundabout"
          else:
            instruction = f"{step_type} {modifier} {name}".strip()

          if dist_m > 0:
            dist_mi = dist_m * M_TO_MI
            if dist_mi >= 1.0:
              instruction += f" ({dist_mi:.1f} mi)"
            else:
              instruction += f" ({dist_m:.0f} m)"

          steps.append({
            "instruction": instruction,
            "distance_m": round(dist_m, 0),
            "duration_s": round(duration_s, 0),
          })

      result = {
        "distance_miles": round(route.get("distance", 0) * M_TO_MI, 1),
        "duration_minutes": round(route.get("duration", 0) / 60, 0),
        "summary": f"{round(route.get('distance', 0) * M_TO_MI, 1)} mi, "
                   f"~{round(route.get('duration', 0) / 60, 0):.0f} min",
        "steps": steps[:15],  # limit to 15 steps for LLM context
      }

      self._cache = result
      self._cache_time = now
      self._cache_key = cache_key
      cloudlog.debug(f"agentd evac: OSRM route: {result['summary']}")
      return result

    except requests.Timeout:
      cloudlog.warning("agentd evac: OSRM API timeout")
      return self._cache
    except requests.RequestException as e:
      cloudlog.warning(f"agentd evac: OSRM API error: {e}")
      return self._cache
    except (json.JSONDecodeError, ValueError, KeyError) as e:
      cloudlog.warning(f"agentd evac: OSRM parse error: {e}")
      return self._cache


# --- Module-level singleton clients ---

_nws_client = NWSClient()
_fdem_client = FDEMClient()
_osrm_client = OSRMClient()


# --- Tool Registration ---

@register_tool(
  name="plan_evacuation",
  description="Check evacuation conditions and plan safe routes during Florida emergencies. "
              "Aggregates NWS weather alerts, FDEM evacuation zones, FL511 road closures, "
              "and OSRM routing to plan routes to shelters or safe destinations. "
              "Use 'check_situation' to assess if evacuation is needed, "
              "'plan_route' to get directions to safety, "
              "or 'find_shelters' to locate nearby emergency shelters."
)
class EvacuationRoutingTool(BaseTool):

  @classmethod
  def schema(cls) -> dict:
    return {
      "name": "plan_evacuation",
      "description": cls.tool_description,
      "input_schema": {
        "type": "object",
        "properties": {
          "action": {
            "type": "string",
            "enum": ["check_situation", "plan_route", "find_shelters"],
            "description": (
              "'check_situation': checks for active weather alerts and whether the "
              "vehicle is in an evacuation zone. "
              "'plan_route': plans an evacuation route to a shelter or safe destination. "
              "'find_shelters': finds nearby emergency shelters with capacity info."
            ),
          },
          "destination_lat": {
            "type": "number",
            "description": "Destination latitude for route planning. If not provided, "
                           "routes to the nearest shelter.",
          },
          "destination_lon": {
            "type": "number",
            "description": "Destination longitude for route planning.",
          },
          "radius_miles": {
            "type": "number",
            "description": "Search radius in miles. Default 50 for situation check, "
                           "25 for shelters.",
          },
        },
        "required": ["action"],
      },
    }

  def execute(self, params: dict, context: dict) -> Advisory:
    gps = context.get("gps", {})
    lat = gps.get("latitude", 0.0)
    lon = gps.get("longitude", 0.0)

    if not gps.get("has_fix") or (lat == 0 and lon == 0):
      return Advisory(
        alert_active=True,
        alert_text="No GPS for evacuation check",
        alert_severity=0,
        tool_name="plan_evacuation",
        reason="no_gps",
      )

    action = params.get("action", "check_situation")

    if action == "check_situation":
      return self._check_situation(lat, lon, params, context)
    elif action == "plan_route":
      return self._plan_route(lat, lon, params, context)
    elif action == "find_shelters":
      return self._find_shelters(lat, lon, params)
    else:
      return Advisory(
        tool_name="plan_evacuation",
        reason=f"Unknown action: {action}",
      )

  def _check_situation(self, lat: float, lon: float,
                       params: dict, context: dict) -> Advisory:
    """Comprehensive evacuation situation assessment."""
    report_parts = []
    max_severity = 0  # 0=info, 1=warning, 2=critical
    evacuation_needed = False

    # 1. NWS Weather Alerts
    alerts = _nws_client.get_alerts(lat, lon)
    evac_alerts = [a for a in alerts if a.get("is_evacuation_relevant")]
    other_alerts = [a for a in alerts if not a.get("is_evacuation_relevant")]

    if evac_alerts:
      report_parts.append("⚠ ACTIVE WEATHER ALERTS:")
      for alert in evac_alerts[:3]:
        sev = NWS_SEVERITY_MAP.get(alert["severity"], 0)
        max_severity = max(max_severity, sev)
        report_parts.append(
          f"  - {alert['event']} ({alert['severity']}): {alert['headline']}"
        )
        if alert.get("instruction"):
          report_parts.append(f"    Instructions: {alert['instruction'][:200]}")
      evacuation_needed = True
    elif other_alerts:
      report_parts.append("Weather alerts (non-evacuation):")
      for alert in other_alerts[:2]:
        report_parts.append(f"  - {alert['event']}: {alert['headline']}")
    else:
      report_parts.append("No active weather alerts.")

    # 2. FDEM Evacuation Zone
    zone = _fdem_client.get_evacuation_zone(lat, lon)
    if zone:
      zone_letter = zone["zone"]
      county = zone["county"]
      status = zone["status"]
      priority = zone["priority"]
      report_parts.append(
        f"\nEvacuation Zone: {zone_letter} ({county} County). Status: {status}"
      )
      if priority <= 2:  # Zone A or B
        report_parts.append(
          f"  ** Zone {zone_letter} is high-priority for evacuation **"
        )
        if evacuation_needed:
          max_severity = 2  # critical
      elif priority <= 4:  # Zone C or D
        report_parts.append(f"  Zone {zone_letter} may require evacuation in severe storms")
    else:
      report_parts.append("\nNot in a designated evacuation zone (or zone data unavailable).")

    # 3. Nearby Evacuation Routes
    routes = _fdem_client.get_nearby_routes(lat, lon, radius_mi=15.0)
    if routes:
      route_names = [r["name"] for r in routes[:5]]
      report_parts.append(f"\nNearby designated evacuation routes: {', '.join(route_names)}")

    # 4. FL511 Road Closures
    fl511_key = Params().get("FL511ApiKey")
    if fl511_key:
      fl511_key = fl511_key.decode("utf-8").strip()
      radius = params.get("radius_miles", 50)
      closures = _fl511_client.get_nearby_events(
        api_key=fl511_key, lat=lat, lon=lon, radius_mi=radius,
      )
      road_closures = [e for e in closures if e.get("EventType") == "closures"]
      if road_closures:
        report_parts.append(f"\n⚠ {len(road_closures)} road closure(s) nearby:")
        for c in road_closures[:3]:
          report_parts.append(
            f"  - {c.get('RoadwayName', '?')} {c.get('DirectionOfTravel', '')}: "
            f"{c.get('Description', '')[:100]} ({c.get('_distance_mi', '?')}mi away)"
          )
        max_severity = max(max_severity, 1)

    # Build alert text
    if evacuation_needed and zone and zone.get("priority", 99) <= 2:
      alert_text = f"EVACUATE: Zone {zone['zone']} active"
    elif evacuation_needed:
      alert_text = "Weather alert: check evacuation"
    elif max_severity >= 1:
      alert_text = "Road closures nearby"
    else:
      alert_text = "No evacuation needed"

    return Advisory(
      alert_active=max_severity > 0 or evacuation_needed,
      alert_text=alert_text[:50],
      alert_severity=max_severity,
      tool_name="plan_evacuation",
      reason="\n".join(report_parts),
    )

  def _plan_route(self, lat: float, lon: float,
                  params: dict, context: dict) -> Advisory:
    """Plan an evacuation route to a safe destination."""
    report_parts = []
    dest_lat = params.get("destination_lat")
    dest_lon = params.get("destination_lon")

    # If no destination, find nearest shelter
    if dest_lat is None or dest_lon is None:
      shelters = _fdem_client.get_nearby_shelters(lat, lon, radius_mi=50.0)
      if shelters:
        best = shelters[0]
        dest_lat = best.get("latitude", 0)
        dest_lon = best.get("longitude", 0)
        report_parts.append(
          f"Routing to nearest shelter: {best['name']} "
          f"({best.get('address', 'Unknown address')}, "
          f"{best.get('distance_mi', '?')}mi away, "
          f"capacity: {best.get('capacity', 'Unknown')})"
        )
      else:
        return Advisory(
          alert_active=True,
          alert_text="No shelters found nearby",
          alert_severity=1,
          tool_name="plan_evacuation",
          reason="No shelters found within 50 miles. Please provide a destination.",
        )

    # Check for road closures on the corridor
    fl511_key = Params().get("FL511ApiKey")
    if fl511_key:
      fl511_key = fl511_key.decode("utf-8").strip()
      # Check midpoint of route for closures
      mid_lat = (lat + dest_lat) / 2
      mid_lon = (lon + dest_lon) / 2
      corridor_radius = _haversine_distance(lat, lon, dest_lat, dest_lon) / 2 + 5
      closures = _fl511_client.get_nearby_events(
        api_key=fl511_key, lat=mid_lat, lon=mid_lon,
        radius_mi=min(corridor_radius, 50),
      )
      road_closures = [e for e in closures
                       if e.get("EventType") in ("closures", "accidentsAndIncidents")]
      if road_closures:
        report_parts.append(f"\n⚠ {len(road_closures)} closure(s)/incident(s) on route corridor:")
        for c in road_closures[:5]:
          report_parts.append(
            f"  - {c.get('RoadwayName', '?')} {c.get('DirectionOfTravel', '')}: "
            f"{c.get('Description', '')[:80]}"
          )

    # Get OSRM route
    route = _osrm_client.get_route(lat, lon, dest_lat, dest_lon)
    if route:
      report_parts.append(f"\nRoute: {route['summary']}")
      report_parts.append("Directions:")
      for i, step in enumerate(route["steps"], 1):
        report_parts.append(f"  {i}. {step['instruction']}")
    else:
      report_parts.append("\nRoute calculation unavailable. Head to shelter at "
                          f"{dest_lat:.4f}, {dest_lon:.4f}")

    # Nearby designated evac routes
    evac_routes = _fdem_client.get_nearby_routes(lat, lon, radius_mi=10.0)
    if evac_routes:
      route_names = [r["name"] for r in evac_routes[:5]]
      report_parts.append(f"\nDesignated evacuation routes nearby: {', '.join(route_names)}")

    # Build advisory
    distance = route["distance_miles"] if route else _haversine_distance(lat, lon, dest_lat, dest_lon)
    summary = route["summary"] if route else f"{distance:.0f}mi to shelter"
    alert_text = f"Evac route: {summary}"

    advisory = Advisory(
      alert_active=True,
      alert_text=alert_text[:50],
      alert_severity=2,  # critical - active evacuation
      tool_name="plan_evacuation",
      reason="\n".join(report_parts),
    )

    # Set speed advisory for evacuation driving
    vehicle = context.get("vehicle", {})
    current_speed = vehicle.get("speed_mph", 45)
    # During evacuation, suggest cautious speed
    evac_speed_mph = min(current_speed, 55)  # cap at 55 mph during evacuation
    advisory.speed_active = True
    advisory.speed_limit_ms = evac_speed_mph * MPH_TO_MS
    advisory.speed_source = "evacuation_routing"
    advisory.speed_confidence = 0.7
    advisory.distance_ahead_m = distance * MI_TO_M if distance else 0

    return advisory

  def _find_shelters(self, lat: float, lon: float, params: dict) -> Advisory:
    """Find nearby emergency shelters."""
    radius = params.get("radius_miles", 25.0)
    shelters = _fdem_client.get_nearby_shelters(lat, lon, radius_mi=radius)

    if not shelters:
      return Advisory(
        alert_active=True,
        alert_text="No shelters found nearby",
        alert_severity=0,
        tool_name="plan_evacuation",
        reason=f"No emergency shelters found within {radius} miles.",
      )

    report_parts = [f"Found {len(shelters)} shelter(s) within {radius} miles:\n"]
    for i, s in enumerate(shelters[:10], 1):
      status = f" [{s['status']}]" if s.get("status") else ""
      pets = " (Pet-friendly)" if s.get("pet_friendly") in ("Yes", "Y", True) else ""
      special = " (Special needs)" if s.get("special_needs") in ("Yes", "Y", True) else ""
      capacity = s.get("capacity", "Unknown")
      avail = s.get("availability")
      avail_str = f", {avail} spots available" if avail is not None else ""

      report_parts.append(
        f"  {i}. {s['name']}{status} — {s.get('distance_mi', '?')}mi\n"
        f"     {s.get('address', 'Unknown address')}, {s.get('county', '')} County\n"
        f"     Capacity: {capacity}{avail_str}{pets}{special}\n"
        f"     GPS: {s.get('latitude', 0):.4f}, {s.get('longitude', 0):.4f}"
      )

    alert_text = f"{len(shelters)} shelter(s) within {radius}mi"

    return Advisory(
      alert_active=True,
      alert_text=alert_text[:50],
      alert_severity=0,  # info
      tool_name="plan_evacuation",
      reason="\n".join(report_parts),
    )
