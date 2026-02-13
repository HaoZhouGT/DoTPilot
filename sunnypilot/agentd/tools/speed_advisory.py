from openpilot.sunnypilot.agentd.tools.base_tool import BaseTool, Advisory
from openpilot.sunnypilot.agentd.tools.registry import register_tool

MPH_TO_MS = 0.44704


@register_tool(
  name="set_speed_advisory",
  description="Set a speed advisory for the vehicle. Use when detecting conditions "
              "that require speed adjustment, such as construction zones, sharp curves, "
              "adverse weather, school zones, heavy traffic, or other hazards ahead."
)
class SpeedAdvisoryTool(BaseTool):

  @classmethod
  def schema(cls) -> dict:
    return {
      "name": "set_speed_advisory",
      "description": cls.tool_description,
      "input_schema": {
        "type": "object",
        "properties": {
          "speed_mph": {
            "type": "number",
            "description": "Recommended target speed in mph (e.g., 45 for a construction zone).",
          },
          "reason": {
            "type": "string",
            "description": "Short explanation of why this speed is recommended (e.g., 'construction_zone', 'school_zone', 'heavy_traffic').",
          },
          "confidence": {
            "type": "number",
            "description": "Confidence level from 0.0 to 1.0 in this advisory.",
          },
          "distance_ahead_m": {
            "type": "number",
            "description": "Approximate distance in meters to the condition requiring the speed change. Defaults to 200m if unknown.",
          },
        },
        "required": ["speed_mph", "reason", "confidence"],
      },
    }

  def execute(self, params: dict, context: dict) -> Advisory:
    speed_ms = params["speed_mph"] * MPH_TO_MS
    return Advisory(
      speed_active=True,
      speed_limit_ms=speed_ms,
      speed_source=params["reason"],
      speed_confidence=params["confidence"],
      distance_ahead_m=params.get("distance_ahead_m", 200.0),
      tool_name="set_speed_advisory",
      reason=params["reason"],
    )
