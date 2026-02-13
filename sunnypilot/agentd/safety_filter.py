import cereal.messaging as messaging
from openpilot.common.swaglog import cloudlog
from openpilot.sunnypilot.agentd.tools.base_tool import Advisory

# Hard safety limits
MIN_SPEED_MS = 0.0           # Can't suggest negative speed
MAX_SPEED_MS = 45.0          # ~100 mph absolute max
MIN_CONFIDENCE = 0.3         # Below this, advisory is discarded
MAX_SPEED_REDUCTION_MS = 20.0  # Can't reduce speed by more than ~45 mph at once
MAX_ALERT_TEXT_LEN = 50


class SafetyFilter:
  """Validates and clamps agent advisory outputs before publishing.

  Ensures the agent can never produce unsafe outputs by enforcing hard limits
  on speed targets, confidence thresholds, and deceleration rates.
  """

  def validate(self, advisory: Advisory | None, sm: messaging.SubMaster) -> Advisory | None:
    if advisory is None:
      return None

    v_ego = sm['carState'].vEgo

    # Validate speed advisory
    if advisory.speed_active:
      advisory = self._validate_speed(advisory, v_ego)

    # Validate lane advisory
    if advisory.lane_active:
      advisory = self._validate_lane(advisory)

    # Validate alert advisory
    if advisory.alert_active:
      advisory = self._validate_alert(advisory)

    return advisory

  def _validate_speed(self, advisory: Advisory, v_ego: float) -> Advisory:
    # Clamp to absolute speed range
    advisory.speed_limit_ms = max(MIN_SPEED_MS, min(advisory.speed_limit_ms, MAX_SPEED_MS))

    # Prevent extreme sudden deceleration
    if v_ego - advisory.speed_limit_ms > MAX_SPEED_REDUCTION_MS:
      cloudlog.warning(
        f"agentd safety: clamping speed reduction from {v_ego:.1f} to "
        f"{advisory.speed_limit_ms:.1f} m/s (max reduction: {MAX_SPEED_REDUCTION_MS:.1f})"
      )
      advisory.speed_limit_ms = v_ego - MAX_SPEED_REDUCTION_MS

    # Confidence check
    advisory.speed_confidence = max(0.0, min(1.0, advisory.speed_confidence))
    if advisory.speed_confidence < MIN_CONFIDENCE:
      cloudlog.info(f"agentd safety: discarding low-confidence speed advisory ({advisory.speed_confidence:.2f})")
      advisory.speed_active = False

    return advisory

  def _validate_lane(self, advisory: Advisory) -> Advisory:
    # Direction must be valid
    if advisory.lane_direction not in (0, 1, 2):
      advisory.lane_active = False
      return advisory

    # Confidence check
    advisory.lane_confidence = max(0.0, min(1.0, advisory.lane_confidence))
    if advisory.lane_confidence < MIN_CONFIDENCE:
      cloudlog.info(f"agentd safety: discarding low-confidence lane advisory ({advisory.lane_confidence:.2f})")
      advisory.lane_active = False

    return advisory

  def _validate_alert(self, advisory: Advisory) -> Advisory:
    # Truncate alert text
    if advisory.alert_text:
      advisory.alert_text = advisory.alert_text[:MAX_ALERT_TEXT_LEN]

    # Severity must be valid
    if advisory.alert_severity not in (0, 1, 2):
      advisory.alert_severity = 0

    return advisory
