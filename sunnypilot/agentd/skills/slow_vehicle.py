from openpilot.sunnypilot.agentd.skills.base_skill import BaseSkill
from openpilot.sunnypilot.agentd.skills.registry import register_skill


@register_skill(
  name="slow_vehicle_handler",
  description="Detects slow-moving vehicles ahead and suggests lane changes when safe."
)
class SlowVehicleSkill(BaseSkill):

  def get_system_prompt_fragment(self) -> str:
    return (
      "When you observe a significantly slower vehicle ahead (e.g., a large truck, "
      "farm equipment, or a vehicle going well below the speed limit) and radar data "
      "confirms the lead vehicle speed is substantially lower than the current cruise "
      "speed, you should:\n"
      "1. If the speed difference is large (>15 mph below cruise speed) and the vehicle "
      "has been slow for the current observation, call set_lane_advisory suggesting "
      "a lane change away from the slow vehicle. Prefer left lane changes on highways.\n"
      "2. If a lane change is not practical (single lane road, both adjacent lanes "
      "occupied), call set_speed_advisory to match the lead vehicle speed with some buffer.\n"
      "3. Consider calling set_alert with 'Slow vehicle ahead' severity 'info' to "
      "inform the driver.\n"
      "Use the radar lead data (leads[0].speed_mph, leads[0].distance_m) to validate "
      "your visual observations. Only suggest lane changes with confidence 0.6+ when "
      "you can visually confirm the adjacent lane appears clear."
    )
