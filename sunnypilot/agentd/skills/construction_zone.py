from openpilot.sunnypilot.agentd.skills.base_skill import BaseSkill
from openpilot.sunnypilot.agentd.skills.registry import register_skill


@register_skill(
  name="construction_zone_handler",
  description="Detects construction zones from camera imagery and adjusts speed/alerts accordingly."
)
class ConstructionZoneSkill(BaseSkill):

  def get_system_prompt_fragment(self) -> str:
    return (
      "When you detect visual indicators of a construction zone in the camera image -- "
      "such as orange traffic cones, construction barrels, 'Road Work Ahead' signs, "
      "lane closure signs, arrow boards, workers in high-visibility vests, heavy equipment "
      "near the road, or temporary lane markings -- you should:\n"
      "1. Call set_alert with text 'Construction zone ahead' and severity 'warning'.\n"
      "2. Call set_speed_advisory to reduce speed to the posted construction zone limit. "
      "If no posted limit is visible, use 45 mph on highways or 25 mph on local roads.\n"
      "3. If the construction narrows lanes or blocks a lane, consider calling "
      "set_lane_advisory to suggest moving away from the work zone.\n"
      "Use high confidence (0.8+) when signs are clearly visible, moderate (0.5-0.8) "
      "when indicators are partial or distant."
    )
