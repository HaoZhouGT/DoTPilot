from openpilot.sunnypilot.agentd.skills.base_skill import BaseSkill
from openpilot.sunnypilot.agentd.skills.registry import register_skill


@register_skill(
  name="fl511_traffic_awareness",
  description="Uses the Florida 511 API to proactively check for traffic events "
              "ahead and advise the driver on speed and lane changes."
)
class FL511TrafficSkill(BaseSkill):

  def get_system_prompt_fragment(self) -> str:
    return (
      "You have access to real-time Florida 511 traffic data via the get_traffic_ahead tool. "
      "Use it proactively to improve the driving experience:\n\n"
      "**When to call get_traffic_ahead:**\n"
      "- Periodically (every few observations) to check for incidents ahead\n"
      "- When you see brake lights, slowing traffic, or congestion in the camera image\n"
      "- When road signs indicate upcoming construction, detours, or incidents\n"
      "- When the vehicle is on a major Florida highway (I-95, I-75, I-4, Florida Turnpike, etc.)\n\n"
      "**How to use it:**\n"
      "1. Use action='check' first to get a summary of nearby events. Review the results.\n"
      "2. If serious events are found (accidents, closures, major roadwork), use action='advise' "
      "to automatically generate speed and alert advisories.\n"
      "3. Set roadway_filter to the current road name if known (from the map data) "
      "to get the most relevant results.\n"
      "4. Set direction_filter to match the vehicle's travel direction.\n\n"
      "**Interpreting severity levels:**\n"
      "- Critical: Major accident, full closure, or severe hazard. Significant speed reduction needed.\n"
      "- Major: Serious incident causing major delays. Moderate speed reduction.\n"
      "- Moderate: Incident with some traffic impact. Mild speed reduction.\n"
      "- Minor: Minor incident, little traffic impact. Alert only.\n\n"
      "**Combining with visual observations:**\n"
      "If you see congestion or construction in the camera AND the FL511 data confirms an "
      "event ahead, use high confidence (0.8+). If only one source indicates an issue, "
      "use moderate confidence (0.5-0.7). Cross-referencing camera + FL511 data gives "
      "the most reliable advisories.\n\n"
      "**Lane change reasoning:**\n"
      "If FL511 reports a closure or accident on the current road and you can see traffic "
      "slowing, consider calling set_lane_advisory to suggest moving to an adjacent lane, "
      "especially if the event description mentions specific lanes being closed."
    )
