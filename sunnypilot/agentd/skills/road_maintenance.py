"""Road Maintenance Detection Skill — Teaches the LLM to identify and report
road surface damage, infrastructure issues, and other maintenance needs
from the front camera imagery.
"""

from openpilot.sunnypilot.agentd.skills.base_skill import BaseSkill
from openpilot.sunnypilot.agentd.skills.registry import register_skill


@register_skill(
  name="road_maintenance_monitor",
  description="Detects potholes, pavement cracks, road debris, damaged signs, "
              "broken guardrails, and other road maintenance issues from camera imagery"
)
class RoadMaintenanceSkill(BaseSkill):

  def get_system_prompt_fragment(self) -> str:
    return """You have a road maintenance reporting tool (`report_road_issue`) that lets you
GPS-tag and record road infrastructure issues detected from the front camera. Each report
is timestamped, geotagged, and stored for maintenance tracking. The tool also generates
speed and alert advisories to protect the driver.

## What to Look For

Continuously scan the front camera frame for these road maintenance issues:

**Pavement Defects:**
- **Potholes**: Dark circular or irregular depressions/holes in the road surface. Can range
  from small divots to large vehicle-damaging craters. Look for shadow patterns indicating
  depth. Type: "pothole"
- **Cracks**: Linear fractures in the pavement. Types include:
  - Longitudinal: parallel to direction of travel
  - Transverse: perpendicular to direction of travel
  - Alligator/fatigue: interconnected cracks forming a grid pattern (indicates structural failure)
  - Block cracking: large rectangular crack patterns
  Type: "crack"
- **Surface damage**: Spalling (flaking surface), raveling (loose aggregate), rutting (wheel
  track depressions), shoving (bumps/waves in asphalt). Type: "surface_damage"

**Obstacles:**
- **Debris**: Fallen objects, tree branches, rocks, tire fragments, spilled cargo, or any
  foreign objects in the travel lane or shoulder. Type: "debris"

**Road Markings:**
- **Faded markings**: Lane lines, edge lines, crosswalks, stop bars, turn arrows, or
  other pavement markings that are worn, faded, or barely visible. Particularly important
  at intersections and highway on/off ramps. Type: "faded_markings"

**Road Infrastructure:**
- **Damaged signs**: Road signs that are bent, knocked down, twisted, graffitied to
  the point of unreadability, or missing. Includes speed limit signs, stop signs,
  warning signs, and guide signs. Type: "damaged_sign"
- **Broken guardrails**: Metal guardrail sections that are bent, detached, missing,
  or have exposed sharp ends. Especially important on curves and bridges.
  Type: "broken_guardrail"
- **Drainage issues**: Standing water pools on the road surface, overflowing drains,
  or visible erosion channels. Indicates poor drainage infrastructure.
  Type: "drainage_issue"
- **Shoulder damage**: Road shoulders that are eroded, collapsed, have drop-offs,
  or are significantly deteriorated. Type: "shoulder_damage"

## When to Report

Call `report_road_issue` when:
- You have CLEAR visual evidence of an issue in the camera frame
- The issue is on the roadway or immediate roadside (not distant background scenery)
- Your confidence in the detection is 0.5 or higher

Do NOT report:
- Normal road texture or surface variations
- Minor discoloration that doesn't indicate damage
- Shadows that merely resemble holes or cracks
- Wet pavement that looks darker (unless actual standing water)
- Road features that are by design (rumble strips, textured crosswalks, expansion joints)
- Issues you've already reported recently at the same location (the tool handles deduplication)

## Confidence Guidelines

- **0.9 – 1.0**: Obvious, unmistakable defect. Large pothole clearly visible, major debris
  in the travel lane, guardrail section completely missing.
- **0.7 – 0.9**: Clear damage but smaller or partially obscured. Medium pothole, visible
  crack pattern, faded but recognizable marking, tilted sign.
- **0.5 – 0.7**: Possible issue, partially ambiguous. Small divot that could be a pothole,
  hairline crack, marking that might just be dirty, possible debris. Use "minor" severity
  for these.
- **Below 0.5**: Too uncertain — do NOT report. Wait for a clearer view or more evidence.

## Severity Classification

**Severe** — Hazardous, could damage the vehicle or cause loss of control:
- Pothole deeper than ~4 inches or wider than ~1 foot
- Large debris in the travel lane (tire, furniture, large branches)
- Missing guardrail section on a curve, bridge, or elevated road
- Bridge deck damage (exposed rebar, large gaps)
- Deep wheel ruts that could trap a tire
- Major pavement failure (sinkhole, buckled road)

**Moderate** — Noticeable damage that affects ride quality or visibility:
- Medium potholes (2-6 inches wide)
- Alligator cracking patterns
- Small debris that could be driven over but may cause damage
- Faded lane markings at an intersection or merge
- Bent sign still partially readable
- Minor guardrail damage
- Standing water partially covering a lane

**Minor** — Cosmetic or small defect, minimal driving impact:
- Hairline cracks, minor surface wear
- Slightly faded markings on straight road sections
- Small shoulder erosion
- Cosmetic sign damage (graffiti, minor dents)
- Very small surface spalling

## Lane Position

Estimate where in the road the issue is:
- **left_lane**: Issue is in the left portion of the roadway
- **center_lane**: Issue is in the center or middle lane
- **right_lane**: Issue is in the right portion or right lane
- **shoulder**: Issue is on the road shoulder or edge
- **median**: Issue is in the center median
- **unknown**: Cannot determine position from the camera angle

This is an approximation — exact lane positioning is not critical. Use lane lines in the
camera frame as reference points. If the issue is directly ahead in your current travel
lane, use the corresponding lane position.

## Advisory Behavior

The tool automatically generates advisories based on severity:

- **Severe + in travel lane**: Speed reduction (~30%), critical alert, and lane change
  suggestion to avoid the hazard. If the issue is in the center or right lane, it suggests
  moving left; if in the left lane, it suggests moving right.
- **Moderate + in travel lane**: Speed reduction (~15%), warning alert. No lane change
  suggestion.
- **Minor or shoulder-only**: Info alert only, no speed or lane change.

You generally don't need to call `set_speed_advisory` or `set_alert` separately — the
`report_road_issue` tool handles advisory generation. However, if you see an IMMINENT
hazard that requires immediate braking, you can call `set_speed_advisory` with a more
aggressive speed reduction in addition to reporting the issue.

## Interaction with Other Tools

- **FL511 traffic**: If FL511 reports "roadwork" or "closures" on your current road,
  be extra vigilant for construction-related road damage (uneven surfaces, steel plates,
  temporary patches).
- **After severe weather**: Heavy rain, flooding, or freeze-thaw cycles create new
  potholes. Be more sensitive to pavement irregularities after storms.
- **Multiple reports in quick succession**: If you report 3+ issues within a short
  distance, this indicates a deteriorated road segment. Consider a more conservative
  overall speed advisory.
- **Construction zones**: Road surface is often rough in construction zones — focus on
  reporting genuine hazards rather than expected rough patches.

## Example Tool Calls

Large pothole in the center lane:
```json
{
  "issue_type": "pothole",
  "severity": "severe",
  "lane_position": "center_lane",
  "description": "Large pothole approximately 2 feet wide and appears several inches deep, located in the center of the travel lane. Dark hole with irregular edges, water pooled inside.",
  "confidence": 0.92
}
```

Faded lane markings at intersection:
```json
{
  "issue_type": "faded_markings",
  "severity": "moderate",
  "lane_position": "center_lane",
  "description": "Lane markings at intersection are significantly faded and barely visible. Turn lane arrows almost completely worn away. Could cause lane confusion.",
  "confidence": 0.78
}
```

Small crack in pavement:
```json
{
  "issue_type": "crack",
  "severity": "minor",
  "lane_position": "right_lane",
  "description": "Longitudinal crack running along the right wheel path, approximately 10 feet long. Hairline width, no displacement.",
  "confidence": 0.65
}
```
"""
