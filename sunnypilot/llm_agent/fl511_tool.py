#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests

from openpilot.common.params import Params

FL511_EVENT_URL = "https://fl511.com/api/v2/get/event"
FL511_TIMEOUT_S = 12
FL511_DEFAULT_LIMIT = 20


@dataclass
class TrafficIncident:
  incident_id: str
  title: str
  road: str
  direction: str
  severity: str
  county: str
  updated_at: str
  lat: float | None
  lon: float | None


def _pick(d: dict[str, Any], *keys: str, default: Any = "") -> Any:
  for k in keys:
    if k in d and d[k] not in (None, ""):
      return d[k]
  return default


def _to_float(v: Any) -> float | None:
  try:
    return float(v)
  except Exception:
    return None


def _normalize_incident(raw: dict[str, Any]) -> TrafficIncident:
  incident_id = str(_pick(raw, "Id", "EventId", "IncidentId", default="unknown"))
  title = str(_pick(raw, "Description", "Headline", "EventText", default="Traffic incident")).strip()
  road = str(_pick(raw, "RoadwayName", "RoadName", "Roadway", default="")).strip()
  direction = str(_pick(raw, "DirectionOfTravel", "Direction", default="")).strip()
  severity = str(_pick(raw, "Severity", "Impact", default="")).strip()
  county = str(_pick(raw, "County", "CountyName", default="")).strip()
  updated_at = str(_pick(raw, "LastUpdated", "LastUpdateTime", "UpdatedTime", default="")).strip()
  lat = _to_float(_pick(raw, "Latitude", "Lat", default=None))
  lon = _to_float(_pick(raw, "Longitude", "Lon", "Lng", default=None))

  return TrafficIncident(
    incident_id=incident_id,
    title=title,
    road=road,
    direction=direction,
    severity=severity,
    county=county,
    updated_at=updated_at,
    lat=lat,
    lon=lon,
  )


def _sort_key(incident: TrafficIncident) -> tuple[int, str]:
  # Keep stable ordering even if timestamp parse fails.
  ts = incident.updated_at or ""
  try:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return (0, dt.isoformat())
  except Exception:
    return (1, ts)


def fetch_fl511_incidents(api_key: str, limit: int = FL511_DEFAULT_LIMIT) -> list[TrafficIncident]:
  params = {
    "key": api_key,
    "format": "json",
  }
  r = requests.get(FL511_EVENT_URL, params=params, timeout=FL511_TIMEOUT_S)
  r.raise_for_status()

  payload = r.json()
  if isinstance(payload, list):
    rows = payload
  elif isinstance(payload, dict):
    rows = payload.get("Data") or payload.get("data") or payload.get("Events") or payload.get("events") or []
  else:
    rows = []

  incidents = [_normalize_incident(x) for x in rows if isinstance(x, dict)]
  incidents = [i for i in incidents if i.title]
  incidents.sort(key=_sort_key, reverse=True)
  return incidents[:max(1, limit)]


def format_incidents_brief(incidents: list[TrafficIncident], max_items: int = 6) -> str:
  if not incidents:
    return "No recent FL511 incidents found."

  lines: list[str] = []
  for inc in incidents[:max_items]:
    parts = [inc.title]
    if inc.road:
      parts.append(inc.road)
    if inc.direction:
      parts.append(inc.direction)
    if inc.county:
      parts.append(inc.county)
    line = " | ".join(parts)
    lines.append(f"- {line}")
  return "\n".join(lines)


def fetch_fl511_incidents_from_params(limit: int = FL511_DEFAULT_LIMIT) -> list[TrafficIncident]:
  params = Params()
  key = params.get("FL511ApiKey")
  key_str = key.decode("utf-8").strip() if isinstance(key, bytes) else str(key or "").strip()
  if not key_str:
    raise RuntimeError("FL511ApiKey is not set")
  return fetch_fl511_incidents(key_str, limit=limit)

