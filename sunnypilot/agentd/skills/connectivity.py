"""Connectivity Awareness Skill — Teaches the LLM when and how to use
the check_connectivity tool and how to adapt behavior based on network conditions.
"""

from openpilot.sunnypilot.agentd.skills.base_skill import BaseSkill
from openpilot.sunnypilot.agentd.skills.registry import register_skill


@register_skill(
  name="connectivity_awareness",
  description="Monitors network connectivity and adapts agent behavior based on "
              "network type, strength, and cloud API reachability."
)
class ConnectivitySkill(BaseSkill):

  def get_system_prompt_fragment(self) -> str:
    return (
      "You receive network connectivity information in the driving context. "
      "You also have the `check_connectivity` tool for detailed status checks "
      "and API endpoint ping tests.\n\n"

      "## Network Context Fields\n\n"
      "The context always includes:\n"
      "- `network_type`: \"none\", \"wifi\", \"cell2G\", \"cell3G\", \"cell4G\", \"cell5G\", \"ethernet\"\n"
      "- `network_strength`: \"unknown\", \"poor\", \"moderate\", \"good\", \"great\"\n"
      "- `network_metered`: true/false (cellular connections are typically metered)\n"
      "- `operator`: carrier name (cellular only, e.g., \"T-Mobile\", \"AT&T\")\n"
      "- `last_athena_ping_s_ago`: seconds since last successful cloud ping (lower is better)\n\n"

      "## When to Call check_connectivity\n\n"
      "Use action=\"status\" (free, no network call):\n"
      "- At the start of a driving session to establish a connectivity baseline\n"
      "- When you notice the context shows weak or no network\n"
      "- Before deciding whether to call network-dependent tools\n\n"
      "Use action=\"ping\" (lightweight HEAD request, cached 30s):\n"
      "- When network_type is not \"none\" but last_athena_ping_s_ago > 120 (stale)\n"
      "- Before calling tools that require external API calls (get_traffic_ahead, "
      "plan_evacuation) when you suspect degraded connectivity\n"
      "- When a previous tool call failed with a timeout or network error\n\n"
      "Do NOT call \"ping\" excessively — once every few minutes is sufficient.\n\n"

      "## Adapting Behavior by Network Quality\n\n"
      "**Disconnected (network_type = \"none\"):**\n"
      "- Do NOT call get_traffic_ahead, plan_evacuation, or any tool requiring internet\n"
      "- Focus on camera-based observations only (road maintenance, visual hazards)\n"
      "- Speed and lane advisories based on visual observations remain valid\n\n"
      "**Very Slow (cell2G, or cell3G with \"poor\" strength):**\n"
      "- Avoid FL511/evacuation checks unless safety-critical\n"
      "- Extend intervals between network-dependent tool calls (every 5+ minutes)\n"
      "- Prioritize camera-based observations\n"
      "- If you must call an API tool, expect possible timeouts\n\n"
      "**Slow/Moderate (cell3G with moderate+ strength, cell4G with \"poor\" strength):**\n"
      "- Reduce FL511 check frequency (every 2-3 minutes instead of every cycle)\n"
      "- Use action=\"check\" not \"advise\" for FL511 to minimize data usage\n"
      "- Skip non-critical evacuation route lookups unless weather is concerning\n\n"
      "**Good (cell4G moderate+, cell5G, wifi good+, ethernet):**\n"
      "- Normal operation — use all tools at standard frequency\n"
      "- FL511 checks every 30-60 seconds as designed\n"
      "- Full evacuation routing available\n\n"
      "**Metered Connection:**\n"
      "- Be data-conscious: fewer API calls, prefer cached results\n"
      "- Skip periodic \"just checking\" traffic queries\n"
      "- Only call evacuation tools when there is genuine weather concern\n\n"

      "## Cross-Tool Interaction\n\n"
      "Network awareness affects how you use other tools:\n"
      "- **FL511 traffic**: Skip periodic checks on poor/no network. FL511 cache is 30s; "
      "if last check was recent, rely on cached data.\n"
      "- **Evacuation routing**: NWS/FDEM/OSRM calls require network. During active weather "
      "with poor connectivity, issue a warning alert recommending the driver check local "
      "radio/TV for evacuation information.\n"
      "- **Road maintenance**: Fully functional without network (uses camera + GPS only). "
      "Prioritize this tool when disconnected.\n"
      "- **Speed/alert/lane advisories**: These are local tools that always work regardless "
      "of network status.\n\n"

      "## Example Scenarios\n\n"
      "Network drops to \"none\" mid-drive:\n"
      "1. Call check_connectivity with action=\"status\" to confirm\n"
      "2. Call set_alert with text=\"Network lost - camera only\" severity info\n"
      "3. Stop calling FL511/evacuation tools\n"
      "4. Continue road maintenance monitoring and visual hazard detection\n\n"
      "Weak cell signal on rural highway:\n"
      "1. Note network_type=\"cell3G\", network_strength=\"poor\" in context\n"
      "2. Reduce FL511 polling to every 3-5 minutes\n"
      "3. If FL511 call times out, don't retry immediately\n"
      "4. Focus on camera-based speed and hazard advisories"
    )
