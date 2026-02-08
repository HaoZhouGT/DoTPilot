from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class Advisory:
  # Speed advisory
  speed_active: bool = False
  speed_limit_ms: float = 0.0
  speed_source: str = ""
  speed_confidence: float = 0.0
  distance_ahead_m: float = 0.0

  # Lane advisory
  lane_active: bool = False
  lane_direction: int = 0  # 0=none, 1=left, 2=right
  lane_reason: str = ""
  lane_confidence: float = 0.0

  # Alert advisory
  alert_active: bool = False
  alert_text: str = ""
  alert_severity: int = 0  # 0=info, 1=warning, 2=critical

  # Meta
  tool_name: str = ""
  reason: str = ""


def merge_advisories(advisories: list[Advisory]) -> Advisory:
  """Merge multiple advisories, taking the most conservative speed and highest severity alert."""
  if not advisories:
    return Advisory()

  result = Advisory()

  for a in advisories:
    # Speed: take the lowest (most conservative) active speed advisory
    if a.speed_active:
      if not result.speed_active or a.speed_limit_ms < result.speed_limit_ms:
        result.speed_active = True
        result.speed_limit_ms = a.speed_limit_ms
        result.speed_source = a.speed_source
        result.speed_confidence = a.speed_confidence
        result.distance_ahead_m = a.distance_ahead_m

    # Lane: take the highest confidence lane advisory
    if a.lane_active:
      if not result.lane_active or a.lane_confidence > result.lane_confidence:
        result.lane_active = True
        result.lane_direction = a.lane_direction
        result.lane_reason = a.lane_reason
        result.lane_confidence = a.lane_confidence

    # Alert: take the highest severity alert
    if a.alert_active:
      if not result.alert_active or a.alert_severity > result.alert_severity:
        result.alert_active = True
        result.alert_text = a.alert_text
        result.alert_severity = a.alert_severity

  # Collect tool names
  tool_names = [a.tool_name for a in advisories if a.tool_name]
  result.tool_name = ",".join(tool_names) if tool_names else ""

  reasons = [a.reason for a in advisories if a.reason]
  result.reason = "; ".join(reasons) if reasons else ""

  return result


class BaseTool(ABC):
  tool_name: str = ""
  tool_description: str = ""

  @classmethod
  @abstractmethod
  def schema(cls) -> dict:
    """Return tool schema for LLM function calling (Anthropic tool_use format)."""
    ...

  @abstractmethod
  def execute(self, params: dict, context: dict) -> Advisory:
    """Execute the tool with given parameters and driving context."""
    ...
