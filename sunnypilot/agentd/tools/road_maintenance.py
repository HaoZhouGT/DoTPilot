"""Road Maintenance Detection & Reporting Tool.

Provides the AI agent with a tool to record road maintenance issues detected
from camera imagery. The LLM sees the camera frame, identifies pavement damage
or broken infrastructure, and calls this tool to GPS-tag, timestamp, and
persist a structured maintenance report.

Reports are stored in both:
  - Params ("RoadMaintenanceReports") — latest 100, for quick agent access
  - JSONL log file on disk — unlimited history, exportable

No external APIs required. Detection is done by the LLM's vision capability.
"""

import json
import os
import time
import uuid
from datetime import datetime, timezone

from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from openpilot.sunnypilot.agentd.tools.base_tool import BaseTool, Advisory
from openpilot.sunnypilot.agentd.tools.registry import register_tool
from openpilot.sunnypilot.agentd.tools.fl511_traffic import _haversine_distance

# --- Constants ---

MPH_TO_MS = 0.44704
MI_TO_M = 1609.34

PARAMS_KEY = "RoadMaintenanceReports"
MAX_PARAMS_REPORTS = 100

# Log file locations (try /data/media first, fall back to /tmp)
LOG_DIR_PRIMARY = "/data/media"
LOG_DIR_FALLBACK = "/tmp"
LOG_FILENAME = "road_maintenance_log.jsonl"

# Deduplication parameters
DEDUP_RADIUS_MI = 0.031  # ~50 meters in miles
DEDUP_WINDOW_S = 300     # 5 minutes

# Valid issue types
VALID_ISSUE_TYPES = {
  "pothole", "crack", "surface_damage", "debris",
  "faded_markings", "damaged_sign", "broken_guardrail",
  "drainage_issue", "shoulder_damage", "other",
}

VALID_SEVERITIES = {"minor", "moderate", "severe"}

VALID_LANE_POSITIONS = {
  "left_lane", "center_lane", "right_lane",
  "shoulder", "median", "unknown",
}

# Severity → advisory mapping
SEVERITY_CONFIG = {
  "severe": {
    "speed_reduction_pct": 0.30,   # 30% speed reduction
    "alert_severity": 2,           # critical
    "confidence_boost": 0.0,       # use LLM's confidence as-is
  },
  "moderate": {
    "speed_reduction_pct": 0.15,   # 15% speed reduction
    "alert_severity": 1,           # warning
    "confidence_boost": 0.0,
  },
  "minor": {
    "speed_reduction_pct": 0.0,    # no speed change
    "alert_severity": 0,           # info
    "confidence_boost": 0.0,
  },
}

# Human-readable labels for issue types
ISSUE_LABELS = {
  "pothole": "Pothole",
  "crack": "Pavement crack",
  "surface_damage": "Surface damage",
  "debris": "Road debris",
  "faded_markings": "Faded markings",
  "damaged_sign": "Damaged sign",
  "broken_guardrail": "Broken guardrail",
  "drainage_issue": "Drainage issue",
  "shoulder_damage": "Shoulder damage",
  "other": "Road issue",
}


# --- Persistence ---

class MaintenanceReportStore:
  """Dual-persistence store for road maintenance reports.

  Reports are stored in both Params (latest N, fast access) and
  an append-only JSONL log file on disk (unlimited history).
  """

  def __init__(self):
    self._params = Params()
    self._log_path = self._resolve_log_path()
    self._recent_reports: list[dict] = []
    self._loaded = False

  def _resolve_log_path(self) -> str:
    """Find a writable location for the log file."""
    if os.path.isdir(LOG_DIR_PRIMARY) and os.access(LOG_DIR_PRIMARY, os.W_OK):
      return os.path.join(LOG_DIR_PRIMARY, LOG_FILENAME)
    cloudlog.info(f"agentd maintenance: {LOG_DIR_PRIMARY} not writable, using {LOG_DIR_FALLBACK}")
    return os.path.join(LOG_DIR_FALLBACK, LOG_FILENAME)

  def _load_from_params(self) -> list[dict]:
    """Load recent reports from Params."""
    if self._loaded:
      return self._recent_reports

    try:
      raw = self._params.get(PARAMS_KEY)
      if raw:
        self._recent_reports = json.loads(raw.decode("utf-8"))
      else:
        self._recent_reports = []
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as e:
      cloudlog.warning(f"agentd maintenance: failed to load reports from Params: {e}")
      self._recent_reports = []

    self._loaded = True
    return self._recent_reports

  def add_report(self, report: dict) -> bool:
    """Save a report to both Params and the JSON log file.

    Returns True if saved, False if duplicate detected.
    """
    # Dedup check
    if self._is_duplicate(report):
      cloudlog.debug(f"agentd maintenance: duplicate report skipped at "
                     f"{report.get('latitude', 0):.4f}, {report.get('longitude', 0):.4f}")
      return False

    # Save to Params (latest N)
    self._save_to_params(report)

    # Append to JSONL log file
    self._append_to_log(report)

    cloudlog.info(
      f"agentd maintenance: recorded {report['issue_type']} ({report['severity']}) "
      f"at {report.get('latitude', 0):.4f}, {report.get('longitude', 0):.4f} "
      f"on {report.get('road_name', 'unknown road')}"
    )
    return True

  def _save_to_params(self, report: dict):
    """Save to Params, keeping the latest MAX_PARAMS_REPORTS entries."""
    try:
      reports = self._load_from_params()
      reports.append(report)

      # Trim to max size
      if len(reports) > MAX_PARAMS_REPORTS:
        reports = reports[-MAX_PARAMS_REPORTS:]

      self._recent_reports = reports
      self._params.put(PARAMS_KEY, json.dumps(reports))
    except Exception as e:
      cloudlog.warning(f"agentd maintenance: failed to save to Params: {e}")

  def _append_to_log(self, report: dict):
    """Append one report as a JSON line to the log file."""
    try:
      with open(self._log_path, "a") as f:
        f.write(json.dumps(report) + "\n")
    except (OSError, IOError) as e:
      cloudlog.warning(f"agentd maintenance: failed to write log file: {e}")

  def _is_duplicate(self, report: dict) -> bool:
    """Check if a similar report was already made nearby and recently."""
    now = report.get("timestamp_mono", time.monotonic())
    lat = report.get("latitude", 0)
    lon = report.get("longitude", 0)
    issue_type = report.get("issue_type", "")

    if lat == 0 and lon == 0:
      return False  # Can't dedup without GPS

    reports = self._load_from_params()
    for existing in reports:
      # Same issue type?
      if existing.get("issue_type") != issue_type:
        continue

      # Recent enough?
      existing_time = existing.get("timestamp_mono", 0)
      if (now - existing_time) > DEDUP_WINDOW_S:
        continue

      # Close enough?
      e_lat = existing.get("latitude", 0)
      e_lon = existing.get("longitude", 0)
      if e_lat == 0 and e_lon == 0:
        continue

      dist = _haversine_distance(lat, lon, e_lat, e_lon)
      if dist <= DEDUP_RADIUS_MI:
        return True

    return False

  def get_recent_reports(self, n: int = 10) -> list[dict]:
    """Get the most recent N reports."""
    reports = self._load_from_params()
    return reports[-n:]

  def get_nearby_reports(self, lat: float, lon: float,
                         radius_mi: float = 1.0) -> list[dict]:
    """Get reports near a GPS point."""
    reports = self._load_from_params()
    nearby = []
    for r in reports:
      r_lat = r.get("latitude", 0)
      r_lon = r.get("longitude", 0)
      if r_lat == 0 and r_lon == 0:
        continue
      dist = _haversine_distance(lat, lon, r_lat, r_lon)
      if dist <= radius_mi:
        r["_distance_mi"] = round(dist, 2)
        nearby.append(r)
    nearby.sort(key=lambda x: x.get("_distance_mi", 999))
    return nearby


# --- Module-level singleton store ---

_store = MaintenanceReportStore()


# --- Tool Registration ---

@register_tool(
  name="report_road_issue",
  description="Report a road maintenance issue detected from the front camera imagery. "
              "Call this when you observe pavement damage (potholes, cracks, surface deterioration), "
              "road debris, faded lane markings, damaged signs, broken guardrails, drainage problems, "
              "or other road infrastructure issues. The report is GPS-tagged, timestamped, and "
              "stored for maintenance tracking. Also generates speed and alert advisories "
              "for the driver based on issue severity."
)
class RoadMaintenanceTool(BaseTool):

  @classmethod
  def schema(cls) -> dict:
    return {
      "name": "report_road_issue",
      "description": cls.tool_description,
      "input_schema": {
        "type": "object",
        "properties": {
          "issue_type": {
            "type": "string",
            "enum": sorted(VALID_ISSUE_TYPES),
            "description": (
              "Type of road maintenance issue: "
              "'pothole' (holes in pavement), "
              "'crack' (longitudinal/transverse/alligator cracking), "
              "'surface_damage' (spalling, raveling, rutting), "
              "'debris' (objects/branches/rocks in travel lane), "
              "'faded_markings' (worn lane lines, crosswalks, stop bars), "
              "'damaged_sign' (bent, missing, or unreadable signs), "
              "'broken_guardrail' (damaged or missing guardrail sections), "
              "'drainage_issue' (standing water, blocked drains), "
              "'shoulder_damage' (eroded or collapsed shoulders), "
              "'other' (any other road infrastructure issue)."
            ),
          },
          "severity": {
            "type": "string",
            "enum": ["minor", "moderate", "severe"],
            "description": (
              "'minor': cosmetic or small defect, does not affect driving. "
              "'moderate': noticeable damage that affects ride quality or visibility. "
              "'severe': hazardous — could damage vehicle or cause loss of control."
            ),
          },
          "lane_position": {
            "type": "string",
            "enum": sorted(VALID_LANE_POSITIONS),
            "description": "Where in the road the issue is located, relative to lane lines.",
          },
          "description": {
            "type": "string",
            "description": "Detailed visual description of the issue as observed in the camera frame.",
          },
          "confidence": {
            "type": "number",
            "description": "Confidence in the detection, 0.0 to 1.0. "
                           "Use 0.9+ for obvious large defects, 0.7-0.9 for clear damage, "
                           "0.5-0.7 for possible issues.",
          },
        },
        "required": ["issue_type", "severity", "description", "confidence"],
      },
    }

  def execute(self, params: dict, context: dict) -> Advisory:
    # --- Extract and validate inputs ---
    issue_type = params.get("issue_type", "other")
    if issue_type not in VALID_ISSUE_TYPES:
      issue_type = "other"

    severity = params.get("severity", "minor")
    if severity not in VALID_SEVERITIES:
      severity = "minor"

    lane_position = params.get("lane_position", "unknown")
    if lane_position not in VALID_LANE_POSITIONS:
      lane_position = "unknown"

    description = params.get("description", "")[:500]
    confidence = max(0.0, min(1.0, params.get("confidence", 0.5)))

    # --- Extract context ---
    gps = context.get("gps", {})
    has_fix = gps.get("has_fix", False)
    lat = gps.get("latitude", 0.0) if has_fix else 0.0
    lon = gps.get("longitude", 0.0) if has_fix else 0.0

    vehicle = context.get("vehicle", {})
    map_data = context.get("map", {})

    # --- Build report ---
    now_mono = time.monotonic()
    report = {
      "id": str(uuid.uuid4()),
      "timestamp": datetime.now(timezone.utc).isoformat(),
      "timestamp_mono": now_mono,
      "latitude": lat,
      "longitude": lon,
      "accuracy_m": gps.get("accuracy_m", 0.0) if has_fix else 0.0,
      "bearing_deg": gps.get("bearing_deg", 0.0) if has_fix else 0.0,
      "speed_mph": vehicle.get("speed_mph", 0.0),
      "road_name": map_data.get("road_name", ""),
      "issue_type": issue_type,
      "severity": severity,
      "lane_position": lane_position,
      "description": description,
      "confidence": confidence,
    }

    # --- Persist report ---
    is_new = _store.add_report(report)

    # --- Build advisory ---
    issue_label = ISSUE_LABELS.get(issue_type, "Road issue")
    sev_config = SEVERITY_CONFIG.get(severity, SEVERITY_CONFIG["minor"])
    road_name = report["road_name"]

    # Reason text (detailed, for LLM / logging)
    reason_parts = [
      f"{'Recorded' if is_new else 'Duplicate skipped'}: {issue_label} ({severity})",
      f"Location: {lat:.6f}, {lon:.6f}" if has_fix else "Location: no GPS fix",
    ]
    if road_name:
      reason_parts.append(f"Road: {road_name}")
    reason_parts.append(f"Lane: {lane_position}")
    reason_parts.append(f"Confidence: {confidence:.0%}")
    reason_parts.append(f"Description: {description}")
    if is_new:
      reason_parts.append(f"Report ID: {report['id']}")

    # Alert text (short, for HUD display, max 50 chars)
    if severity == "severe":
      alert_text = f"{issue_label} ahead - caution"
    elif severity == "moderate":
      alert_text = f"{issue_label} reported"
    else:
      alert_text = f"{issue_label} noted"

    advisory = Advisory(
      alert_active=True,
      alert_text=alert_text[:50],
      alert_severity=sev_config["alert_severity"],
      tool_name="report_road_issue",
      reason="\n".join(reason_parts),
    )

    # Speed reduction for moderate and severe issues in travel lanes
    if severity in ("severe", "moderate") and lane_position in ("left_lane", "center_lane", "right_lane", "unknown"):
      reduction_pct = sev_config["speed_reduction_pct"]
      current_speed = vehicle.get("speed_mph", 55)
      cruise_mph = vehicle.get("cruise_set_mph", 60)
      base_speed = max(current_speed, cruise_mph)
      target_mph = base_speed * (1.0 - reduction_pct)
      target_ms = target_mph * MPH_TO_MS

      advisory.speed_active = True
      advisory.speed_limit_ms = target_ms
      advisory.speed_source = f"road_maintenance_{issue_type}"
      advisory.speed_confidence = confidence
      advisory.distance_ahead_m = 100.0  # Approximate — issue is visible in frame

    # Lane advisory for severe issues blocking a specific lane
    if severity == "severe" and lane_position in ("center_lane", "right_lane"):
      advisory.lane_active = True
      advisory.lane_direction = 1  # suggest left
      advisory.lane_reason = f"{issue_label} in {lane_position.replace('_', ' ')}"
      advisory.lane_confidence = confidence * 0.8  # slightly lower than detection confidence
    elif severity == "severe" and lane_position == "left_lane":
      advisory.lane_active = True
      advisory.lane_direction = 2  # suggest right
      advisory.lane_reason = f"{issue_label} in left lane"
      advisory.lane_confidence = confidence * 0.8

    return advisory
