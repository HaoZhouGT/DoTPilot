"""Evacuation Routing Skill — Teaches the LLM when and how to use
the plan_evacuation tool for Florida emergency evacuation scenarios.
"""

from openpilot.sunnypilot.agentd.skills.base_skill import BaseSkill
from openpilot.sunnypilot.agentd.skills.registry import register_skill


@register_skill(
  name="evacuation_routing",
  description="Guides evacuation decision-making using NWS alerts, FDEM zones, "
              "FL511 road closures, and OSRM routing during Florida emergencies"
)
class EvacuationRoutingSkill(BaseSkill):

  def get_system_prompt_fragment(self) -> str:
    return """You have access to a powerful evacuation routing tool (`plan_evacuation`) that
aggregates data from NWS Weather Alerts, FDEM Evacuation Zones, FL511 road closures,
and OSRM routing to help drivers navigate Florida emergency situations safely.

## When to Check for Evacuations

Call `plan_evacuation` with action="check_situation" when:
- During hurricane season (June 1 – November 30), especially if you observe:
  - Heavy rain, strong winds, or rapidly deteriorating weather
  - Flooding on the roadway or standing water
  - Unusually heavy traffic all moving in the same direction (possible evacuation flow)
  - Contra-flow lanes (lanes reversed for evacuation traffic)
  - Emergency vehicles or National Guard presence
  - Road signs showing evacuation route markers or weather warnings
- If the FL511 tool reports "closures" or "Critical" severity events
- If the driver appears to be on a major Florida evacuation corridor:
  - I-75 (Tampa to Georgia), I-95 (Miami to Jacksonville), I-4 (Tampa to Orlando),
  - Florida Turnpike, I-10 (Pensacola to Jacksonville), US-1 (Florida Keys)
- Every 5-10 inference cycles during active weather events as a periodic check

## Two-Phase Evacuation Assessment

**Phase 1: Situation Check**
Always start with action="check_situation" to get a comprehensive picture:
- Active NWS weather alerts (Hurricane Warning, Flood Warning, Storm Surge, etc.)
- Current FDEM evacuation zone (A through F)
- Nearby road closures from FL511
- Designated evacuation routes in the area

**Phase 2: Route Planning (if evacuation needed)**
If Phase 1 reveals:
- Hurricane Warning or Storm Surge Warning AND driver is in Zone A or B → IMMEDIATELY call
  action="plan_route" and issue critical alert via set_alert("EVACUATE NOW", "critical")
- Tropical Storm Warning AND Zone A–C → call action="plan_route" as a precaution
- Flood Warning AND visual flooding observed → call action="plan_route"
- Multiple road closures AND severe weather → call action="plan_route"

If no immediate danger, use action="find_shelters" to know nearby shelter options.

## Understanding Evacuation Zones

Florida evacuation zones indicate vulnerability to storm surge:
- **Zone A**: Most vulnerable. Coastal areas, barrier islands, mobile homes. Evacuate first.
- **Zone B**: Next most vulnerable. Low-lying areas near coast.
- **Zone C**: Moderate vulnerability. Further inland but still at risk in strong storms.
- **Zone D**: Lower vulnerability. Inland areas at risk in major hurricanes.
- **Zone E / F**: Least vulnerable. Typically only for Category 4-5 hurricanes.
- **No Zone**: Far enough inland to not require mandatory evacuation for storm surge.

Some counties use additional zone codes (T, L, AB, BC, etc.) — treat these as
variations of the standard A–F classification.

## Making Advisory Decisions

**Speed Advisories during Evacuation:**
- On evacuation routes with heavy traffic: limit to current traffic flow speed
- During heavy rain/flooding: suggest 35-45 mph even on highways
- On clear evacuation routes: allow up to 55 mph
- Near road closures or detours: suggest 25-35 mph
- Confidence: 0.8+ when multiple data sources confirm danger, 0.5-0.7 for single source

**Alert Advisories:**
- "EVACUATE: Zone [X] active" → severity "critical" (hurricane/surge warning + zone A/B)
- "Evacuation advisory: Zone [X]" → severity "warning" (tropical storm + zone A-C)
- "Weather alert: monitor" → severity "info" (watches, distant threats)
- "Evac route: [distance/time]" → severity "critical" (active routing)

**Lane Advisories:**
- Suggest lane changes to get onto designated evacuation routes
- If camera shows contra-flow lanes, note this for driver awareness
- Confidence 0.6+ when combining route data with visual observations

## Cross-Referencing Data Sources

For highest confidence, combine multiple sources:
- NWS alert + FDEM zone A/B + visible flooding → confidence 0.9+ (evacuate immediately)
- NWS alert + FDEM zone C/D → confidence 0.7 (prepare to evacuate)
- NWS watch (not warning) only → confidence 0.4 (monitor, no immediate action)
- FL511 closures only (no NWS alert) → use get_traffic_ahead for standard traffic handling
- Visual observation only (flooding, debris) → confidence 0.5, call check_situation to verify

## Route Planning Considerations

When the tool plans a route:
- It automatically finds the nearest shelter if no destination is specified
- It checks for FL511 road closures along the route corridor
- It provides turn-by-turn OSRM directions
- Designated FDEM evacuation routes are noted — prefer these routes
- During active evacuations, expect delays and plan for longer travel times

## Important Safety Notes

- The evacuation tool is ADVISORY ONLY — the driver makes all final decisions
- Never suppress or delay a critical evacuation alert
- If in doubt about whether to evacuate, err on the side of caution
- Evacuation conditions change — re-check periodically during active weather events
- The tool uses public API data; during extreme events, APIs may be slow or unavailable
- If APIs are unreachable, issue a warning alert and recommend the driver check local media
"""
