#!/usr/bin/env python3
from __future__ import annotations

import math
import re
import time
from datetime import datetime, timezone
from typing import Any

try:
  from cereal import log
  import cereal.messaging as messaging

  from openpilot.common.params import Params
  from openpilot.common.realtime import Ratekeeper
  from openpilot.common.swaglog import cloudlog
except ImportError:
  log = None
  messaging = None
  Params = None
  Ratekeeper = None
  cloudlog = None

try:
  import requests
except ImportError:
  requests = None


FL511_BASE_URL = "https://fl511.com"
TRAFFIC_ADVISORY_PARAM = "TrafficAdvisory"
SCHEMA_VERSION = 1

POLL_INTERVAL_S = 60.0
HTTP_TIMEOUT_S = 8.0
LOCAL_LOG_PATH = "/data/traffic-advisor/traffic_advisor_runtime.log"

SEARCH_RADIUS_M = 8000.0
CLOSE_RADIUS_M = 800.0
AHEAD_BEARING_DEG = 75.0
ROUTE_CORRIDOR_M = 500.0
MAX_DETAIL_FETCHES = 24
MAX_NEARBY_ADVISORIES = 5
FUTURE_EVENT_LOOKAHEAD_S = 2.0 * 60.0 * 60.0

FLORIDA_BOUNDS = {
  "min_lat": 24.0,
  "max_lat": 31.2,
  "min_lon": -87.8,
  "max_lon": -79.6,
}

LAYER_INFO = {
  "Closures": {"type": "closure", "label": "Closure", "priority": 90},
  "Incidents": {"type": "incident", "label": "Incident", "priority": 75},
  "Construction": {"type": "construction", "label": "Work zone", "priority": 55},
  "Congestion": {"type": "congestion", "label": "Congestion", "priority": 45},
  "DisabledVehicles": {"type": "disabled_vehicle", "label": "Disabled vehicle", "priority": 45},
  "RoadConditionIncident": {"type": "road_condition", "label": "Road condition", "priority": 45},
  "WeatherIncidents": {"type": "weather", "label": "Weather", "priority": 40},
  "WeatherEvents": {"type": "weather", "label": "Weather", "priority": 35},
  "SpecialEvents": {"type": "special_event", "label": "Event traffic", "priority": 25},
  "MessageSigns": {"type": "message_sign", "label": "Message sign", "priority": 20},
}

SEVERITY_SCORE = {
  "critical": 100,
  "severe": 90,
  "major": 80,
  "high": 80,
  "intermediate": 55,
  "moderate": 55,
  "medium": 55,
  "minor": 25,
  "low": 25,
  "info": 10,
  "unknown": 0,
}

DIRECTION_ALIASES = {
  "n": "north",
  "northbound": "north",
  "north": "north",
  "s": "south",
  "southbound": "south",
  "south": "south",
  "e": "east",
  "eastbound": "east",
  "east": "east",
  "w": "west",
  "westbound": "west",
  "west": "west",
  "both": "both",
  "both directions": "both",
}


def _log_local(message: str) -> None:
  try:
    import os

    os.makedirs(os.path.dirname(LOCAL_LOG_PATH), exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOCAL_LOG_PATH, "a", encoding="utf-8") as f:
      f.write(f"{ts} {message}\n")
  except Exception:
    pass


def _clean_text(value: Any, max_len: int, default: str = "") -> str:
  if value is None:
    return default
  text = " ".join(str(value).replace("\n", " ").split())
  return text[:max_len] if text else default


def _float_value(value: Any, default: float = 0.0) -> float:
  try:
    return float(value)
  except (TypeError, ValueError):
    return default


def _in_florida(lat: float, lon: float) -> bool:
  return (
    FLORIDA_BOUNDS["min_lat"] <= lat <= FLORIDA_BOUNDS["max_lat"] and
    FLORIDA_BOUNDS["min_lon"] <= lon <= FLORIDA_BOUNDS["max_lon"]
  )


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
  r = 6371000.0
  p1 = math.radians(lat1)
  p2 = math.radians(lat2)
  dp = math.radians(lat2 - lat1)
  dl = math.radians(lon2 - lon1)
  a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
  return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
  p1 = math.radians(lat1)
  p2 = math.radians(lat2)
  dl = math.radians(lon2 - lon1)
  y = math.sin(dl) * math.cos(p2)
  x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
  return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _bearing_delta(a: float | None, b: float | None) -> float | None:
  if a is None or b is None:
    return None
  return abs((a - b + 180.0) % 360.0 - 180.0)


def _distance_text(distance_m: float) -> str:
  miles = distance_m / 1609.344
  if miles < 0.15:
    return "nearby"
  if miles < 10.0:
    return f"{miles:.1f} mi"
  return f"{round(miles)} mi"


def _severity(value: Any, is_full_closure: bool = False) -> str:
  if is_full_closure:
    return "major"
  severity = _clean_text(value, 32, "unknown").lower()
  return severity if severity in SEVERITY_SCORE else "unknown"


def _direction(value: Any) -> str:
  direction = _clean_text(value, 32).lower()
  return DIRECTION_ALIASES.get(direction, direction)


def _parse_time(value: Any) -> datetime | None:
  text = _clean_text(value, 64)
  if not text:
    return None
  try:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
  except ValueError:
    return None


def _normalize_road_name(value: Any) -> str:
  text = _clean_text(value, 80).upper()
  replacements = (
    ("INTERSTATE", "I"),
    ("STATE ROAD", "SR"),
    ("STATE ROUTE", "SR"),
    ("US HIGHWAY", "US"),
    ("HIGHWAY", "HWY"),
    ("SOUTHBOUND", ""),
    ("NORTHBOUND", ""),
    ("EASTBOUND", ""),
    ("WESTBOUND", ""),
  )
  for old, new in replacements:
    text = text.replace(old, new)
  return re.sub(r"[^A-Z0-9]", "", text)


def _road_names_match(a: Any, b: Any) -> bool:
  na = _normalize_road_name(a)
  nb = _normalize_road_name(b)
  return bool(na and nb and (na == nb or na in nb or nb in na))


def _parse_wkt_point(value: Any) -> tuple[float, float] | None:
  text = _clean_text(value, 120)
  match = re.search(r"POINT\s*\(\s*([-0-9.]+)\s+([-0-9.]+)\s*\)", text, re.IGNORECASE)
  if not match:
    return None
  lon = _float_value(match.group(1), float("nan"))
  lat = _float_value(match.group(2), float("nan"))
  if math.isfinite(lat) and math.isfinite(lon):
    return lat, lon
  return None


def _lat_lon_from_detail(detail: dict[str, Any]) -> tuple[float, float] | None:
  lat = _float_value(detail.get("latitude"), float("nan"))
  lon = _float_value(detail.get("longitude"), float("nan"))
  if math.isfinite(lat) and math.isfinite(lon) and (lat != 0.0 or lon != 0.0):
    return lat, lon

  try:
    wkt = detail["latLng"]["geography"]["wellKnownText"]
  except (KeyError, TypeError):
    return None
  return _parse_wkt_point(wkt)


def _lat_lon_from_icon(icon: dict[str, Any]) -> tuple[float, float] | None:
  loc = icon.get("location")
  if not isinstance(loc, list) or len(loc) < 2:
    return None
  lat = _float_value(loc[0], float("nan"))
  lon = _float_value(loc[1], float("nan"))
  if math.isfinite(lat) and math.isfinite(lon):
    return lat, lon
  return None


def _decode_polyline(encoded: Any) -> list[tuple[float, float]]:
  text = _clean_text(encoded, 10000)
  coords: list[tuple[float, float]] = []
  index = 0
  lat = 0
  lon = 0

  while index < len(text):
    result = 0
    shift = 0
    while index < len(text):
      b = ord(text[index]) - 63
      index += 1
      result |= (b & 0x1f) << shift
      shift += 5
      if b < 0x20:
        break
    lat += ~(result >> 1) if result & 1 else result >> 1

    result = 0
    shift = 0
    while index < len(text):
      b = ord(text[index]) - 63
      index += 1
      result |= (b & 0x1f) << shift
      shift += 5
      if b < 0x20:
        break
    lon += ~(result >> 1) if result & 1 else result >> 1
    coords.append((lat * 1e-5, lon * 1e-5))

  return coords


def _min_distance_to_points(lat: float, lon: float, points: list[tuple[float, float]]) -> float | None:
  if not points:
    return None
  return min(_haversine_m(lat, lon, point_lat, point_lon) for point_lat, point_lon in points)


def _clean_dms_message(value: Any) -> str:
  text = _clean_text(value, 500)
  text = re.sub(r"\[nl\]", " / ", text, flags=re.IGNORECASE)
  text = re.sub(r"\[np\]", " | ", text, flags=re.IGNORECASE)
  text = re.sub(r"\[[^\]]+\]", "", text)
  return _clean_text(text.replace(" /  | ", " | "), 140)


def _is_marker_relevant(distance_m: float, bearing_delta_deg: float | None, route_distance_m: float | None = None) -> bool:
  if route_distance_m is not None and route_distance_m <= ROUTE_CORRIDOR_M and distance_m <= SEARCH_RADIUS_M:
    return True
  if distance_m <= CLOSE_RADIUS_M:
    return True
  if distance_m > SEARCH_RADIUS_M:
    return False
  return bearing_delta_deg is None or bearing_delta_deg <= AHEAD_BEARING_DEG


def _is_time_relevant(advisory: dict[str, Any]) -> bool:
  now = datetime.now(timezone.utc)
  start = _parse_time(advisory.get("start_time"))
  end = _parse_time(advisory.get("end_time"))
  if end is not None and now > end:
    return False
  if start is not None and start > now:
    return (start - now).total_seconds() <= FUTURE_EVENT_LOOKAHEAD_S
  return True


def _driver_display(advisory: dict[str, Any]) -> str:
  layer_label = _clean_text(advisory.get("label"), 32, "Advisory")
  road = _clean_text(advisory.get("roadway"), 32)
  distance = _distance_text(_float_value(advisory.get("distance_m")))

  pieces = [f"{layer_label} ahead"]
  if road:
    pieces.append(f"on {road}")
  pieces.append(distance)
  return _clean_text(" - ".join(pieces), 72, "Traffic advisory ahead")


def _normalize_detail(layer: str, detail: dict[str, Any], vehicle_lat: float, vehicle_lon: float,
                      bearing_delta_deg: float | None, route_distance_m: float | None,
                      current_road: str) -> dict[str, Any] | None:
  layer_info = LAYER_INFO[layer]
  detail_pos = _lat_lon_from_detail(detail)
  if detail_pos is None:
    return None

  event_lat, event_lon = detail_pos
  point_distance_m = _haversine_m(vehicle_lat, vehicle_lon, event_lat, event_lon)
  polyline_points = _decode_polyline(detail.get("polyline"))
  polyline_distance_m = _min_distance_to_points(vehicle_lat, vehicle_lon, polyline_points)
  distance_m = min(point_distance_m, polyline_distance_m) if polyline_distance_m is not None else point_distance_m

  if not _is_marker_relevant(distance_m, bearing_delta_deg, route_distance_m):
    return None

  is_full_closure = bool(detail.get("isFullClosure"))
  advisory_type = layer_info["type"]
  roadway = _clean_text(detail.get("roadway") or detail.get("roadwayName") or detail.get("name"), 80)
  severity = _severity(detail.get("severity"), is_full_closure)
  lane_description = _clean_text(detail.get("laneDescription"), 72)

  if advisory_type == "message_sign":
    message = _clean_dms_message(detail.get("messages"))
    if not message:
      return None
  else:
    message = _clean_text(detail.get("description") or detail.get("comment"), 180)

  if not message:
    message = layer_info["label"]

  last_updated = (
    detail.get("lastUpdated") or detail.get("lastUpdate") or detail.get("lastComm") or detail.get("startDate")
  )

  advisory: dict[str, Any] = {
    "source": "fl511",
    "layer": layer,
    "id": str(detail.get("id", "")),
    "source_id": _clean_text(detail.get("sourceId"), 64),
    "agency_source": _clean_text(detail.get("source"), 64),
    "type": advisory_type,
    "label": layer_info["label"],
    "severity": severity,
    "roadway": roadway,
    "direction": _direction(detail.get("direction")),
    "message": message,
    "lane_description": lane_description,
    "latitude": event_lat,
    "longitude": event_lon,
    "distance_m": round(distance_m, 1),
    "bearing_delta_deg": round(bearing_delta_deg, 1) if bearing_delta_deg is not None else None,
    "route_distance_m": round(route_distance_m, 1) if route_distance_m is not None else None,
    "start_time": _clean_text(detail.get("startDate"), 64),
    "end_time": _clean_text(detail.get("endDate"), 64),
    "last_updated": _clean_text(last_updated, 64),
    "area": _clean_text((detail.get("area") or {}).get("areaName", {}).get("text") if isinstance(detail.get("area"), dict) else "", 48),
    "camera_ids": _clean_text(detail.get("cameraIds"), 120),
    "is_full_closure": is_full_closure,
    "same_road": _road_names_match(current_road, roadway),
  }

  if not _is_time_relevant(advisory):
    return None

  score = float(layer_info["priority"]) + float(SEVERITY_SCORE.get(severity, 0))
  if advisory["same_road"]:
    score += 35.0
  if is_full_closure:
    score += 25.0
  if route_distance_m is not None and route_distance_m <= ROUTE_CORRIDOR_M:
    score += 20.0
  if bearing_delta_deg is not None and bearing_delta_deg <= AHEAD_BEARING_DEG:
    score += 12.0
  score -= min(distance_m / 160.0, 50.0)
  advisory["priority_score"] = round(score, 2)
  advisory["driver_display"] = _driver_display(advisory)
  return advisory


def _route_points(nav_route: Any) -> list[tuple[float, float]]:
  points: list[tuple[float, float]] = []
  try:
    coordinates = nav_route.coordinates
  except Exception:
    return points

  for coord in coordinates:
    lat = _float_value(coord.latitude, float("nan"))
    lon = _float_value(coord.longitude, float("nan"))
    if math.isfinite(lat) and math.isfinite(lon):
      points.append((lat, lon))
  return points


class TrafficAdvisor:
  def __init__(self):
    if requests is None:
      raise RuntimeError("requests is required for traffic advisor")

    self.session = requests.Session()
    self.session.headers.update({
      "Accept": "application/json, text/javascript, */*; q=0.01",
      "Referer": FL511_BASE_URL + "/",
      "User-Agent": "DoTPilot traffic-advisor prototype",
      "X-Requested-With": "XMLHttpRequest",
    })
    self.detail_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}

  def _get_json(self, path: str) -> Any:
    response = self.session.get(FL511_BASE_URL + path, timeout=HTTP_TIMEOUT_S)
    response.raise_for_status()
    return response.json()

  def _get_layer_icons(self, layer: str) -> list[dict[str, Any]]:
    data = self._get_json(f"/map/mapIcons/{layer}")
    icons = data.get("item2") if isinstance(data, dict) else None
    return icons if isinstance(icons, list) else []

  def _get_detail(self, layer: str, item_id: str) -> dict[str, Any] | None:
    cache_key = (layer, item_id)
    cached = self.detail_cache.get(cache_key)
    now = time.monotonic()
    if cached is not None and now - cached[0] < POLL_INTERVAL_S * 2.0:
      return cached[1]

    data = self._get_json(f"/map/data/{layer}/{item_id}")
    if not isinstance(data, dict):
      return None
    self.detail_cache[cache_key] = (now, data)
    return data

  def poll(self, vehicle_lat: float, vehicle_lon: float, vehicle_bearing: float | None,
           current_road: str, route_points: list[tuple[float, float]]) -> dict[str, Any]:
    layer_status: dict[str, Any] = {}
    marker_candidates: list[dict[str, Any]] = []

    for layer in LAYER_INFO:
      try:
        icons = self._get_layer_icons(layer)
      except Exception as e:
        layer_status[layer] = {"error": _clean_text(e, 120)}
        continue

      relevant = 0
      for icon in icons:
        icon_pos = _lat_lon_from_icon(icon)
        if icon_pos is None:
          continue
        event_lat, event_lon = icon_pos
        distance_m = _haversine_m(vehicle_lat, vehicle_lon, event_lat, event_lon)
        event_bearing = _bearing_deg(vehicle_lat, vehicle_lon, event_lat, event_lon)
        bearing_delta = _bearing_delta(vehicle_bearing, event_bearing)
        route_distance = _min_distance_to_points(event_lat, event_lon, route_points)
        if not _is_marker_relevant(distance_m, bearing_delta, route_distance):
          continue
        relevant += 1
        marker_candidates.append({
          "layer": layer,
          "item_id": str(icon.get("itemId", "")),
          "distance_m": distance_m,
          "bearing_delta_deg": bearing_delta,
          "route_distance_m": route_distance,
        })

      layer_status[layer] = {"icons": len(icons), "nearby": relevant}

    marker_candidates.sort(
      key=lambda c: (
        c["distance_m"] - LAYER_INFO[c["layer"]]["priority"] * 20.0,
        c["distance_m"],
      )
    )

    advisories: list[dict[str, Any]] = []
    for candidate in marker_candidates[:MAX_DETAIL_FETCHES]:
      item_id = _clean_text(candidate["item_id"], 64)
      if not item_id:
        continue
      try:
        detail = self._get_detail(candidate["layer"], item_id)
      except Exception as e:
        layer_status.setdefault(candidate["layer"], {})["detail_error"] = _clean_text(e, 120)
        continue
      if detail is None:
        continue

      advisory = _normalize_detail(
        candidate["layer"],
        detail,
        vehicle_lat,
        vehicle_lon,
        candidate["bearing_delta_deg"],
        candidate["route_distance_m"],
        current_road,
      )
      if advisory is not None:
        advisories.append(advisory)

    advisories.sort(key=lambda a: _float_value(a.get("priority_score")), reverse=True)
    selected = advisories[0] if advisories else None

    return {
      "schema_version": SCHEMA_VERSION,
      "source": "fl511",
      "timestamp": time.time(),
      "has_advisory": selected is not None,
      "driver_display": selected.get("driver_display") if selected else "",
      "selected": selected or {},
      "nearby": advisories[:MAX_NEARBY_ADVISORIES],
      "position": {
        "latitude": vehicle_lat,
        "longitude": vehicle_lon,
        "bearing": vehicle_bearing,
        "road_name": current_road,
      },
      "status": {
        "layers": layer_status,
        "candidate_count": len(marker_candidates),
        "advisory_count": len(advisories),
      },
    }


def _read_position(sm: messaging.SubMaster) -> tuple[float, float, float | None] | None:
  location = sm["liveLocationKalman"]
  if location.status != log.LiveLocationKalman.Status.valid or not location.positionGeodetic.valid or not location.gpsOK:
    return None

  lat = _float_value(location.positionGeodetic.value[0], float("nan"))
  lon = _float_value(location.positionGeodetic.value[1], float("nan"))
  if not math.isfinite(lat) or not math.isfinite(lon):
    return None

  bearing = None
  if location.calibratedOrientationNED.valid:
    bearing = (math.degrees(_float_value(location.calibratedOrientationNED.value[2])) + 360.0) % 360.0
  return lat, lon, bearing


def _no_advisory(reason: str) -> dict[str, Any]:
  return {
    "schema_version": SCHEMA_VERSION,
    "source": "fl511",
    "timestamp": time.time(),
    "has_advisory": False,
    "driver_display": "",
    "selected": {},
    "nearby": [],
    "status": {"reason": reason},
  }


def main() -> None:
  if Params is None or Ratekeeper is None or cloudlog is None or log is None or messaging is None:
    raise RuntimeError("openpilot runtime dependencies are required for traffic advisor")

  params = Params()
  sm = messaging.SubMaster(["liveLocationKalman", "liveMapDataSP", "navRoute"])
  advisor = TrafficAdvisor()
  rk = Ratekeeper(1.0)

  cloudlog.info("traffic-advisor: starting")
  last_poll = 0.0
  last_heartbeat = 0.0

  while True:
    sm.update(0)
    now = time.monotonic()

    if now - last_heartbeat >= 60.0:
      cloudlog.info(f"traffic-advisor: alive (enabled={params.get_bool('TrafficAdvisorEnabled')})")
      last_heartbeat = now

    if now - last_poll >= POLL_INTERVAL_S:
      last_poll = now
      position = _read_position(sm)
      if position is None:
        params.put_nonblocking(TRAFFIC_ADVISORY_PARAM, _no_advisory("no_valid_location"))
        rk.keep_time()
        continue

      lat, lon, bearing = position
      if not _in_florida(lat, lon):
        params.put_nonblocking(TRAFFIC_ADVISORY_PARAM, _no_advisory("outside_florida"))
        rk.keep_time()
        continue

      current_road = _clean_text(sm["liveMapDataSP"].roadName if sm.valid["liveMapDataSP"] else "", 80)
      route = _route_points(sm["navRoute"]) if sm.valid["navRoute"] else []

      try:
        payload = advisor.poll(lat, lon, bearing, current_road, route)
        params.put_nonblocking(TRAFFIC_ADVISORY_PARAM, payload)
        if payload.get("has_advisory"):
          selected = payload.get("selected", {})
          cloudlog.info(f"traffic-advisor: {payload.get('driver_display')} ({selected.get('id')})")
        _log_local(f"poll ok has_advisory={payload.get('has_advisory')} status={payload.get('status')}")
      except Exception as e:
        cloudlog.warning(f"traffic-advisor: poll failed: {e}")
        _log_local(f"poll failed: {e}")
        params.put_nonblocking(TRAFFIC_ADVISORY_PARAM, _no_advisory("poll_failed"))

    rk.keep_time()


if __name__ == "__main__":
  main()
