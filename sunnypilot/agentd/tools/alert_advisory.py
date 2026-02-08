from openpilot.sunnypilot.agentd.tools.base_tool import BaseTool, Advisory
from openpilot.sunnypilot.agentd.tools.registry import register_tool

SEVERITY_MAP = {
  "info": 0,
  "warning": 1,
  "critical": 2,
}


@register_tool(
  name="set_alert",
  description="Show an alert message to the driver on the HUD. Use for important "
              "situational awareness information such as construction zone ahead, "
              "emergency vehicle approaching, unusual road conditions, or any other "
              "hazard the driver should be aware of."
)
class AlertAdvisoryTool(BaseTool):

  @classmethod
  def schema(cls) -> dict:
    return {
      "name": "set_alert",
      "description": cls.tool_description,
      "input_schema": {
        "type": "object",
        "properties": {
          "text": {
            "type": "string",
            "description": "Short alert text to display to the driver (max 50 characters).",
          },
          "severity": {
            "type": "string",
            "enum": ["info", "warning", "critical"],
            "description": "Alert severity level.",
          },
        },
        "required": ["text", "severity"],
      },
    }

  def execute(self, params: dict, context: dict) -> Advisory:
    text = params["text"][:50]  # Enforce max length
    severity = SEVERITY_MAP.get(params["severity"], 0)
    return Advisory(
      alert_active=True,
      alert_text=text,
      alert_severity=severity,
      tool_name="set_alert",
      reason=text,
    )
