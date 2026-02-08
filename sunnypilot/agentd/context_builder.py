import base64

import cereal.messaging as messaging

MS_TO_MPH = 2.23694
KPH_TO_MPH = 0.621371


class ContextBuilder:
  """Builds a structured driving context dict from subscribed messages.

  This context is sent to the LLM to provide situational awareness.
  All values are converted to human-readable units (mph, meters, degrees).
  """

  def build(self, sm: messaging.SubMaster, frame_jpeg: bytes | None) -> dict:
    return {
      "vehicle": self._build_vehicle(sm),
      "leads": self._build_leads(sm),
      "model": self._build_model(sm),
      "map": self._build_map(sm),
      "system": self._build_system(sm),
      "frame_jpeg_b64": base64.b64encode(frame_jpeg).decode() if frame_jpeg else None,
    }

  def _build_vehicle(self, sm: messaging.SubMaster) -> dict:
    cs = sm['carState']
    return {
      "speed_mph": round(cs.vEgo * MS_TO_MPH, 1),
      "acceleration_ms2": round(cs.aEgo, 2),
      "steering_angle_deg": round(cs.steeringAngleDeg, 1),
      "cruise_set_mph": round(cs.vCruise * KPH_TO_MPH, 1),
      "cruise_enabled": cs.cruiseState.enabled,
      "brake_pressed": cs.brakePressed,
      "gas_pressed": cs.gasPressed,
      "left_blinker": cs.leftBlinker,
      "right_blinker": cs.rightBlinker,
      "standstill": cs.standstill,
      "gear": str(cs.gearShifter),
    }

  def _build_leads(self, sm: messaging.SubMaster) -> list[dict]:
    leads = []
    radar = sm['radarState']

    for i, lead in enumerate([radar.leadOne, radar.leadTwo]):
      if not lead.status:
        continue
      leads.append({
        "index": i,
        "distance_m": round(lead.dRel, 1),
        "speed_mph": round(lead.vLead * MS_TO_MPH, 1),
        "relative_speed_mph": round(lead.vRel * MS_TO_MPH, 1),
        "acceleration_ms2": round(lead.aLeadK, 2),
        "lateral_offset_m": round(lead.yRel, 1),
      })

    return leads

  def _build_model(self, sm: messaging.SubMaster) -> dict:
    model = sm['modelV2']
    meta = model.meta
    return {
      "lane_change_prob": round(meta.laneChangeProb, 3),
      "desire_state": str(meta.desireState),
    }

  def _build_map(self, sm: messaging.SubMaster) -> dict:
    map_data = sm['liveMapDataSP']
    result = {
      "road_name": str(map_data.roadName) if map_data.roadName else "",
    }

    if map_data.speedLimitValid:
      result["speed_limit_mph"] = round(map_data.speedLimit * KPH_TO_MPH, 0)

    if map_data.speedLimitAheadValid:
      result["speed_limit_ahead_mph"] = round(map_data.speedLimitAhead * KPH_TO_MPH, 0)
      result["speed_limit_ahead_distance_m"] = round(map_data.speedLimitAheadDistance, 0)

    return result

  def _build_system(self, sm: messaging.SubMaster) -> dict:
    ss = sm['selfdriveState']
    return {
      "openpilot_enabled": ss.enabled,
      "openpilot_active": ss.active,
    }
