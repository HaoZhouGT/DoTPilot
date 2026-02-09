# Road Maintenance Detection & Reporting Tool

The Road Maintenance Detection Tool turns every DoTPilot-equipped vehicle into a road condition surveyor. It leverages the AI agent's existing vision capability — the LLM already receives periodic camera snapshots — to detect potholes, pavement cracks, road debris, damaged signs, broken guardrails, and other infrastructure issues. Each detection is GPS-tagged, timestamped, and persisted as a structured maintenance report while simultaneously generating speed and alert advisories to protect the driver.

## How It Works

Unlike traditional road condition monitoring that requires dedicated computer vision models, this tool uses a **zero-additional-cost** approach: the LLM (Claude) already receives front camera frames as part of every inference cycle. The companion skill teaches it what road damage looks like, and the tool gives it a structured way to record what it sees.

```
  Camera frame (1 Hz JPEG) ──────────────────┐
                                              │
  GPS + vehicle state ─────┐                  ▼
                           │        ┌──────────────────┐
                           │        │   LLM observes   │
                           │        │   road damage    │
                           │        │   in frame       │
                           │        └────────┬─────────┘
                           │                 │
                           │    tool_use: report_road_issue(
                           │      issue_type="pothole",
                           │      severity="severe",
                           │      lane_position="center_lane",
                           │      description="Large pothole ~2ft wide..."
                           │      confidence=0.92
                           │    )
                           │                 │
                           ▼                 ▼
                    ┌─────────────────────────────────┐
                    │      RoadMaintenanceTool         │
                    ├─────────────────────────────────┤
                    │  1. Tag with GPS + timestamp     │
                    │  2. Deduplication check           │
                    │  3. Save to Params (latest 100)  │
                    │  4. Append to JSONL log file      │
                    │  5. Return Advisory               │
                    └──────────┬──────────────────────┘
                               │
                    ┌──────────┼──────────┐
                    ▼          ▼          ▼
                 Speed      Alert      Lane
                advisory   advisory   advisory
```

## Issue Types

The tool classifies road maintenance issues into 10 categories:

| Issue Type | Description | Visual Signature |
|------------|-------------|-----------------|
| `pothole` | Holes or depressions in pavement | Dark circular/irregular shapes with shadow indicating depth |
| `crack` | Linear fractures in pavement | Longitudinal, transverse, alligator, or block crack patterns |
| `surface_damage` | Spalling, raveling, rutting, shoving | Flaking surface, loose aggregate, wheel ruts, asphalt waves |
| `debris` | Foreign objects in travel lane | Tree branches, rocks, tire fragments, spilled cargo |
| `faded_markings` | Worn lane lines, crosswalks, stop bars | Barely visible paint, particularly at intersections |
| `damaged_sign` | Bent, missing, or unreadable signs | Knocked-over poles, twisted sign faces, graffiti obscuring text |
| `broken_guardrail` | Damaged or missing guardrail | Bent metal, detached sections, exposed sharp ends |
| `drainage_issue` | Standing water, blocked drains | Water pools on road surface, visible erosion channels |
| `shoulder_damage` | Eroded or collapsed shoulders | Drop-offs, missing material, deteriorated road edges |
| `other` | Any other infrastructure issue | Catch-all for items not covered above |

## Severity Levels

| Severity | Description | Examples | Advisory |
|----------|-------------|----------|----------|
| **Severe** | Hazardous — could damage vehicle or cause loss of control | Pothole >1ft wide, large debris in lane, missing guardrail on curve, bridge deck damage | 30% speed reduction + critical alert + lane change suggestion |
| **Moderate** | Noticeable damage affecting ride quality or visibility | Medium potholes, alligator cracking, faded intersection markings, minor debris | 15% speed reduction + warning alert |
| **Minor** | Cosmetic or small defect, minimal driving impact | Hairline cracks, slight surface wear, faded markings on straight road | Info alert only — no speed or lane change |

Speed reductions are only applied when the issue is in a travel lane (`left_lane`, `center_lane`, `right_lane`, or `unknown`). Issues on the `shoulder` or `median` generate alerts but no speed changes.

## Report Data Structure

Each maintenance report contains:

```json
{
  "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "timestamp": "2025-09-15T14:32:08.123456+00:00",
  "timestamp_mono": 1234567.89,
  "latitude": 25.761680,
  "longitude": -80.191790,
  "accuracy_m": 3.2,
  "bearing_deg": 45.0,
  "speed_mph": 42.5,
  "road_name": "I-95",
  "issue_type": "pothole",
  "severity": "severe",
  "lane_position": "center_lane",
  "description": "Large pothole approximately 2 feet wide and several inches deep, located in the center of the travel lane. Dark hole with irregular edges, water pooled inside.",
  "confidence": 0.92
}
```

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | UUID v4 unique identifier |
| `timestamp` | string | ISO 8601 UTC timestamp |
| `timestamp_mono` | float | Monotonic clock time (for deduplication timing) |
| `latitude` | float | GPS latitude (6 decimal places, ~0.1m precision) |
| `longitude` | float | GPS longitude |
| `accuracy_m` | float | GPS accuracy radius in meters |
| `bearing_deg` | float | Vehicle heading at detection time |
| `speed_mph` | float | Vehicle speed at detection time |
| `road_name` | string | Road name from map data (e.g., "I-95", "NW 36th St") |
| `issue_type` | string | One of the 10 issue type categories |
| `severity` | string | `minor`, `moderate`, or `severe` |
| `lane_position` | string | `left_lane`, `center_lane`, `right_lane`, `shoulder`, `median`, or `unknown` |
| `description` | string | LLM's visual description of the issue (max 500 chars) |
| `confidence` | float | LLM's self-assessed detection confidence (0.0–1.0) |

## Persistence

Reports are stored in two locations for different use cases:

### Params Store (Fast Access)

- **Key**: `RoadMaintenanceReports`
- **Format**: JSON array of report objects
- **Capacity**: Latest 100 reports (oldest trimmed when full)
- **Use case**: Quick access by the agent for deduplication checks, recent report queries, and in-session reference

### JSONL Log File (Complete History)

- **Primary path**: `/data/media/road_maintenance_log.jsonl`
- **Fallback path**: `/tmp/road_maintenance_log.jsonl` (if `/data/media` is not writable)
- **Format**: One JSON object per line (JSONL), append-only
- **Capacity**: Unlimited — grows with each new report
- **Use case**: Export for analysis, historical road condition mapping, submission to transportation agencies

The JSONL format makes the log file easy to process:

```bash
# Count total reports
wc -l road_maintenance_log.jsonl

# Filter severe potholes
cat road_maintenance_log.jsonl | python3 -c "
import sys, json
for line in sys.stdin:
    r = json.loads(line)
    if r['issue_type'] == 'pothole' and r['severity'] == 'severe':
        print(f\"{r['timestamp']} | {r['latitude']:.4f}, {r['longitude']:.4f} | {r['description'][:60]}\")
"

# Export as CSV
cat road_maintenance_log.jsonl | python3 -c "
import sys, json, csv
writer = csv.DictWriter(sys.stdout, fieldnames=['timestamp','latitude','longitude','road_name','issue_type','severity','description'])
writer.writeheader()
for line in sys.stdin:
    writer.writerow(json.loads(line))
" > reports.csv
```

## Deduplication

Driving slowly past the same pothole — or stopping in traffic next to one — could generate dozens of duplicate reports. The deduplication system prevents this:

- **Spatial threshold**: 50 meters (~0.031 miles)
- **Temporal threshold**: 5 minutes
- **Issue type match**: Required (a pothole and a crack at the same location are different reports)

A new report is considered a duplicate if an existing report with the **same issue type** was recorded within **50 meters** in the last **5 minutes**. Duplicates are silently skipped (logged at debug level) and the advisory is still returned to alert the driver.

Reports without GPS fix (latitude/longitude = 0) bypass deduplication since there's no location to compare.

## Advisory Behavior

The tool automatically generates three types of advisories based on severity and lane position:

### Speed Advisory

| Condition | Speed Reduction | Confidence |
|-----------|----------------|------------|
| Severe + in travel lane | 30% of current/cruise speed | LLM's confidence value |
| Moderate + in travel lane | 15% of current/cruise speed | LLM's confidence value |
| Minor or shoulder/median | No speed change | — |

The speed reduction is calculated from `max(current_speed, cruise_set_speed)` and converted to m/s for the planner. The safety filter ensures the final speed stays within 0–100 mph and doesn't drop more than 45 mph at once.

### Alert Advisory

| Severity | Alert Text Example | HUD Severity |
|----------|-------------------|-------------|
| Severe | "Pothole ahead - caution" | Critical (red) |
| Moderate | "Pavement crack reported" | Warning (yellow) |
| Minor | "Faded markings noted" | Info (white) |

Alert text is capped at 50 characters by the safety filter.

### Lane Change Advisory

Only generated for **severe** issues in a specific travel lane:

| Issue Position | Suggested Direction |
|---------------|-------------------|
| `center_lane` or `right_lane` | Move left |
| `left_lane` | Move right |
| `shoulder`, `median`, `unknown` | No lane change |

Lane change confidence is set to 80% of the detection confidence (slightly conservative since lane position is an approximation).

## Confidence Calibration

The companion skill teaches the LLM to calibrate its confidence:

| Range | Meaning | Typical Detections |
|-------|---------|-------------------|
| **0.9–1.0** | Unmistakable | Large pothole clearly visible, major debris in lane, missing guardrail |
| **0.7–0.9** | Clear but smaller | Medium pothole, visible crack pattern, tilted sign, faded marking |
| **0.5–0.7** | Possible, ambiguous | Small divot, hairline crack, dirty marking, possible debris |
| **Below 0.5** | Too uncertain | Not reported — wait for clearer evidence |

The safety filter discards any advisory with confidence below 0.3, providing an additional safety net.

## Configuration

| Parameter | Description | Required |
|-----------|-------------|----------|
| `AgentEnabled` | Enable the AI agent daemon | Yes |
| `AgentApiKey` | Anthropic API key for LLM backend | Yes |

No additional API keys or configuration needed — the tool uses only on-device resources (GPS, camera, local storage).

## Tool Schema

The LLM invokes the tool with:

```json
{
  "name": "report_road_issue",
  "input": {
    "issue_type": "pothole",
    "severity": "severe",
    "lane_position": "center_lane",
    "description": "Large pothole approximately 2 feet wide...",
    "confidence": 0.92
  }
}
```

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_type` | string enum | Yes | One of the 10 issue categories |
| `severity` | string enum | Yes | `minor`, `moderate`, or `severe` |
| `lane_position` | string enum | No | Where in the road (defaults to `unknown`) |
| `description` | string | Yes | Visual description from camera observation |
| `confidence` | number | Yes | Detection confidence, 0.0–1.0 |

## Example Scenarios

### Severe Pothole in Travel Lane

The LLM observes a large, dark depression in the center of the lane with visible depth:

```
Tool call: report_road_issue(
  issue_type="pothole", severity="severe",
  lane_position="center_lane", confidence=0.92,
  description="Large pothole ~2ft wide, several inches deep, water pooled inside"
)

→ Report saved: ID a1b2c3d4... at 25.7617, -80.1918 on I-95
→ Alert: "Pothole ahead - caution" (critical)
→ Speed: reduced 30% (65 mph → 45.5 mph)
→ Lane: suggest left to avoid center lane hazard
```

### Faded Markings at Intersection

The LLM notices turn lane arrows that are barely visible:

```
Tool call: report_road_issue(
  issue_type="faded_markings", severity="moderate",
  lane_position="center_lane", confidence=0.78,
  description="Turn lane arrows at intersection almost completely worn away"
)

→ Report saved: ID e5f6a7b8... at 28.5383, -81.3792 on Colonial Dr
→ Alert: "Faded markings reported" (warning)
→ Speed: reduced 15% (45 mph → 38.3 mph)
→ No lane change advisory
```

### Small Shoulder Erosion

The LLM spots minor erosion on the road edge:

```
Tool call: report_road_issue(
  issue_type="shoulder_damage", severity="minor",
  lane_position="shoulder", confidence=0.65,
  description="Right shoulder eroded approximately 6 inches, slight drop-off"
)

→ Report saved: ID c9d0e1f2... at 30.3322, -81.6557 on US-1
→ Alert: "Shoulder damage noted" (info)
→ No speed or lane change advisory (shoulder-only, minor severity)
```

### Duplicate Suppression

Vehicle crawling in traffic past the same pothole reported 2 minutes ago, 20 meters back:

```
Tool call: report_road_issue(
  issue_type="pothole", severity="severe", ...
)

→ Duplicate detected (same issue_type within 50m and 5min)
→ Report NOT saved
→ Advisory still returned to keep driver alerted
```

## Cross-Tool Interactions

The skill teaches the LLM to use context from other tools:

- **FL511 reports construction**: Heightened vigilance for uneven surfaces, steel plates, temporary patches in the area
- **After severe weather**: More sensitive to new potholes and debris (freeze-thaw, flooding)
- **Multiple reports in succession**: 3+ issues in a short distance suggests a deteriorated road segment — the LLM may issue a more conservative speed advisory
- **Construction zones**: Rough surfaces are expected — focus on genuine hazards, not normal construction roughness

## File Structure

```
sunnypilot/agentd/
├── tools/
│   ├── road_maintenance.py      # MaintenanceReportStore + RoadMaintenanceTool
│   ├── evacuation_routing.py    # Evacuation intelligence
│   ├── fl511_traffic.py         # FL511 traffic events
│   ├── speed_advisory.py        # set_speed_advisory
│   ├── lane_advisory.py         # set_lane_advisory
│   ├── alert_advisory.py        # set_alert
│   ├── base_tool.py             # Advisory dataclass, BaseTool ABC
│   ├── registry.py              # @register_tool decorator
│   └── __init__.py
└── skills/
    ├── road_maintenance.py      # LLM prompt for defect detection
    ├── evacuation_routing.py    # Evacuation reasoning
    ├── fl511_traffic.py         # Traffic reasoning
    ├── construction_zone.py     # Construction zone detection
    ├── slow_vehicle.py          # Slow vehicle detection
    ├── base_skill.py            # BaseSkill ABC
    ├── registry.py              # @register_skill decorator
    └── __init__.py
```

## Safety Considerations

- **Advisory-only**: The tool never directly controls the vehicle. All outputs pass through the `SafetyFilter` before reaching the planner.
- **Speed clamping**: 0–45 m/s (0–100 mph) hard limits, maximum 20 m/s (~45 mph) sudden reduction.
- **Confidence threshold**: Advisories below 0.3 confidence are discarded by the safety filter.
- **Conservative lane changes**: Lane change confidence is 80% of detection confidence, providing a buffer for lane position estimation uncertainty.
- **Deduplication prevents alert fatigue**: Drivers won't be bombarded with repeated alerts for the same issue in slow traffic.
- **Graceful degradation**: GPS failure, Params errors, and log file issues are all handled individually — if one persistence layer fails, the other still works.

## Roadmap

- [ ] Export reports to GeoJSON for mapping visualization
- [ ] Integration with FDOT maintenance request submission portal
- [ ] On-device lightweight pothole detection model for higher-frequency scanning
- [ ] Severity re-assessment using vehicle IMU data (bump detection correlated with visual observation)
- [ ] Community aggregation — merge reports from multiple DoTPilot vehicles for confidence boosting
- [ ] Historical heatmap of road condition by segment
- [ ] Automatic 311/SeeClickFix report generation for municipalities
