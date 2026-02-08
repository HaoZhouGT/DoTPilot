from openpilot.sunnypilot.agentd.tools.base_tool import BaseTool, Advisory
from openpilot.sunnypilot.agentd.tools.registry import register_tool

DIRECTION_MAP = {
  "none": 0,
  "left": 1,
  "right": 2,
}


@register_tool(
  name="set_lane_advisory",
  description="Suggest a lane change to the driver. Use when detecting conditions "
              "where changing lanes would improve safety or efficiency, such as a slow "
              "truck ahead, a merge required, or an obstruction in the current lane. "
              "This is advisory only - the driver must confirm via blinker or steering."
)
class LaneAdvisoryTool(BaseTool):

  @classmethod
  def schema(cls) -> dict:
    return {
      "name": "set_lane_advisory",
      "description": cls.tool_description,
      "input_schema": {
        "type": "object",
        "properties": {
          "direction": {
            "type": "string",
            "enum": ["left", "right"],
            "description": "Suggested lane change direction.",
          },
          "reason": {
            "type": "string",
            "description": "Short explanation (e.g., 'slow_truck_ahead', 'merge_required', 'obstruction').",
          },
          "confidence": {
            "type": "number",
            "description": "Confidence level from 0.0 to 1.0.",
          },
        },
        "required": ["direction", "reason", "confidence"],
      },
    }

  def execute(self, params: dict, context: dict) -> Advisory:
    direction = DIRECTION_MAP.get(params["direction"], 0)
    return Advisory(
      lane_active=True,
      lane_direction=direction,
      lane_reason=params["reason"],
      lane_confidence=params["confidence"],
      tool_name="set_lane_advisory",
      reason=params["reason"],
    )
