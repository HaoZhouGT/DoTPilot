# FL511 Traffic Tool

The FL511 Traffic Tool provides the DoTPilot AI agent with real-time traffic intelligence from the Florida 511 Advanced Traveler Information System. It queries for nearby accidents, road closures, construction zones, and congestion events, then generates speed and alert advisories to help the driver navigate Florida roadways safely.

This was the first external data tool built for the DoTPilot agent framework and serves as the foundation that other tools (evacuation routing, road maintenance) build upon — its `FL511Client` singleton and `_haversine_distance` helper are reused across the toolchain.

## How It Works

```
  Vehicle GPS ──────────┐
                        ▼
                ┌──────────────┐     ┌────────────────────┐
                │  FL511Client │────►│ fl511.com/api/     │
                │  (cached)    │◄────│ getevents          │
                └──────┬───────┘     └────────────────────┘
                       │
              Nearby events filtered
              by distance + direction
              + roadway name
                       │
                       ▼
              ┌──────────────────┐
              │  get_traffic_ahead│
              │  tool            │
              ├──────────────────┤
              │ action="check"   │──► Event summary for LLM reasoning
              │ action="advise"  │──► Auto-generated speed/alert Advisory
              └────────┬─────────┘
                       │
                       ▼
              Advisory → Safety Filter → Planner
```

The LLM can invoke this tool in two modes:

1. **`check`** — Returns a structured summary of the 5 closest traffic events for the LLM to reason about. The LLM decides what action to take.
2. **`advise`** — Automatically generates speed reduction and alert advisories based on the most severe nearby event. Used when the LLM has already confirmed a situation needs action.

## Data Source

### Florida 511 API

- **Endpoint**: `https://fl511.com/api/getevents`
- **Authentication**: API key (register at [fl511.com/developers](https://fl511.com/developers))
- **Rate limit**: 10 calls per 60 seconds
- **Cache TTL**: 30 seconds (shared singleton client)
- **Response format**: JSON array of traffic event objects
- **Coverage**: Florida interstates, toll roads (Florida's Turnpike), and major metropolitan roadways

FL511 aggregates data from FDOT sensors, SunGuide traffic management, Road Rangers, Florida Highway Patrol, Waze, and local law enforcement.

### Event Fields

Each event from the API contains:

| Field | Description |
|-------|-------------|
| `EventType` | Category of event (see event types table below) |
| `Severity` | `Critical`, `Major`, `Moderate`, `Minor`, or `Unknown` |
| `RoadwayName` | Road where the event is located (e.g., "I-95", "Florida Turnpike") |
| `DirectionOfTravel` | `Northbound`, `Southbound`, `Eastbound`, `Westbound` |
| `Description` | Human-readable description of the event |
| `Location` | Location description (e.g., "At Exit 42 — SR 826") |
| `LanesAffected` | Which lanes are affected |
| `Latitude` / `Longitude` | GPS coordinates of the event |
| `RegionName` | Florida region (e.g., "Southeast Florida") |
| `CountyName` | County where the event is located |

### Event Types

| EventType | Label | Triggers Speed Reduction |
|-----------|-------|:------------------------:|
| `accidentsAndIncidents` | Accident | Yes |
| `roadwork` | Road work | Yes |
| `closures` | Road closure | Yes |
| `winterDrivingIndex` | Weather hazard | Yes |
| `specialEvents` | Special event | No (alert only) |

## Architecture

### FL511Client (Cached API Client)

The `FL511Client` is a module-level singleton that handles API communication and caching:

```python
_fl511_client = FL511Client()
```

- **`get_events(api_key)`** — Fetches all Florida traffic events. Returns cached results if the last fetch was within 30 seconds. On API failure, returns the previous cache (never returns empty if data was once available).
- **`get_nearby_events(api_key, lat, lon, radius_mi, direction, roadway)`** — Filters events by GPS proximity using haversine distance, with optional direction and roadway name filters. Results sorted by distance (closest first).

This singleton is imported by the evacuation routing tool for road closure checks — sharing the cache and respecting rate limits across tools.

### Haversine Distance

The `_haversine_distance(lat1, lon1, lat2, lon2)` function calculates great-circle distance in miles between two GPS coordinates. This utility is also imported by the evacuation routing and road maintenance tools for their own proximity calculations.

## Severity Mapping

FL511 severity levels map to advisory confidence and speed reduction percentages:

| FL511 Severity | Confidence | Alert Level | Speed Reduction |
|----------------|:----------:|:-----------:|:---------------:|
| **Critical** | 0.9 | Critical (red) | 50% |
| **Major** | 0.8 | Warning (yellow) | 35% |
| **Moderate** | 0.7 | Warning (yellow) | 25% |
| **Minor** | 0.6 | Info (white) | 15% |
| **Unknown** | 0.5 | Info (white) | 10% |

Speed reduction is calculated from `max(current_speed, cruise_set_speed)`. For example, a Critical accident while cruising at 70 mph produces a target of 35 mph (50% reduction).

Speed advisories are only generated for slowdown-worthy event types (accidents, roadwork, closures, weather hazards). Special events generate alerts but no speed changes.

## Configuration

| Parameter | Description | Required |
|-----------|-------------|----------|
| `AgentEnabled` | Enable the AI agent daemon | Yes |
| `AgentApiKey` | Anthropic API key for LLM backend | Yes |
| `FL511ApiKey` | FL511 API key for traffic data | **Yes** |

### Getting an FL511 API Key

1. Visit [fl511.com/developers](https://fl511.com/developers)
2. Register for a developer account
3. Generate an API key
4. Set it in DoTPilot: `params.put("FL511ApiKey", "your-key-here")`

## Tool Schema

The LLM invokes the tool with:

```json
{
  "name": "get_traffic_ahead",
  "input": {
    "action": "check",
    "radius_miles": 10,
    "roadway_filter": "I-95",
    "direction_filter": "Northbound"
  }
}
```

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `action` | string enum | Yes | — | `check` (summary for LLM) or `advise` (auto-generate advisory) |
| `radius_miles` | number | No | 10 | Search radius from vehicle's GPS position |
| `roadway_filter` | string | No | — | Filter to a specific road (e.g., "I-95", "Florida Turnpike") |
| `direction_filter` | string | No | — | Filter by direction (e.g., "Northbound", "Southbound") |

## Two-Phase Usage Pattern

The companion skill teaches the LLM a two-step approach:

### Phase 1: Check

```
LLM calls: get_traffic_ahead(action="check", roadway_filter="I-95")

Response reason:
  FL511: #1 [Critical] accidentsAndIncidents on I-95 Northbound -
  2.3mi away - At Exit 42 - Multi-vehicle accident blocking 2 lanes;
  #2 [Moderate] roadwork on I-95 Northbound - 5.1mi away -
  At mile marker 118 - Lane closure for resurfacing
```

The LLM reviews the event summary and decides on the appropriate response.

### Phase 2: Advise

If the LLM determines action is needed:

```
LLM calls: get_traffic_ahead(action="advise", roadway_filter="I-95")

Response:
  Alert: "Accident on I-95 - 2.3mi ahead" (critical)
  Speed: 35.0 mph (50% reduction from 70 mph cruise, confidence 0.9)
  Reason: FL511 Critical: Accident on I-95 at Exit 42.
          Multi-vehicle accident blocking 2 lanes
```

## Example Outputs

### Check Mode — Multiple Events Found

```
Alert: "3 traffic event(s) nearby" (info)

Reason:
  FL511: #1 [Critical] accidentsAndIncidents on I-95 Northbound -
  2.3mi away - At Exit 42 — SR 826 - Multi-vehicle accident, 2 lanes blocked;
  #2 [Moderate] roadwork on I-95 Northbound - 5.1mi away -
  Mile marker 118 - Right lane closed for resurfacing;
  #3 [Minor] accidentsAndIncidents on I-95 Southbound - 8.7mi away -
  At Exit 38 - Minor fender bender on shoulder
```

### Advise Mode — Critical Accident

```
Alert: "Accident on I-95 - 2.3mi ahead" (critical)
Speed: 35.0 mph target (confidence: 0.9, source: fl511_accidentsAndIncidents)
Distance ahead: 3,702 m

Reason: FL511 Critical: Accident on I-95 at Exit 42 — SR 826.
        Multi-vehicle accident, 2 lanes blocked
```

### Advise Mode — Moderate Roadwork

```
Alert: "Road work on I-4 - 4.5mi ahead" (warning)
Speed: 48.8 mph target (confidence: 0.7, source: fl511_roadwork)
Distance ahead: 7,242 m

Reason: FL511 Moderate: Road work on I-4 at Mile marker 78.
        Right 2 lanes closed for bridge repair
```

### No Events Found

```
(No alert, no speed change)
Reason: no_events_nearby
```

### No API Key Configured

```
Alert: "FL511 API key not configured" (info)
Reason: missing_api_key
```

## How It Was Built

### Design Principles

1. **Singleton cached client**: A single `FL511Client` instance is shared at module level. The 30-second cache respects the FL511 rate limit (10 calls/60 seconds) while keeping data reasonably fresh. On API failure, the cache is returned rather than empty results — stale data is better than no data.

2. **GPS-based proximity filtering**: Rather than returning all statewide events, the tool uses haversine distance to filter events within the search radius. Results are sorted by distance so the closest (most relevant) events come first.

3. **Two action modes**: The `check`/`advise` split gives the LLM flexibility. In `check` mode, it receives raw data and can reason about context (camera observations, vehicle state, other tool outputs). In `advise` mode, the tool handles all the math for speed reduction and alert generation.

4. **Severity-proportional response**: Speed reductions scale with severity — a Critical accident gets 50% reduction while a Minor incident gets only 15%. This prevents both under-reaction to serious events and over-reaction to minor ones.

5. **Graceful degradation**: Every API call is wrapped in try/except. Timeouts, HTTP errors, and JSON parse failures all return cached data with a warning log. The driving system never crashes due to a traffic API failure.

### Shared Utilities

Two components from this tool are imported by other tools:

- **`_fl511_client`** — The singleton FL511Client instance is imported by the evacuation routing tool for road closure checks along evacuation corridors.
- **`_haversine_distance()`** — The haversine distance function is imported by both the evacuation routing tool (shelter distance, route corridor width) and the road maintenance tool (deduplication radius).

This makes `fl511_traffic.py` a foundational module in the agent toolchain.

## Cross-Tool Interactions

- **Evacuation routing**: Imports `_fl511_client` directly to check for road closures along evacuation corridors. Shares the cache, so no additional API calls are needed.
- **Road maintenance**: If FL511 reports roadwork on the current road, the road maintenance skill teaches the LLM to be extra vigilant for construction-related surface damage.
- **Construction zone skill**: FL511 roadwork events corroborate visual construction zone detection, boosting confidence when both sources agree.

## File Structure

```
sunnypilot/agentd/
├── tools/
│   ├── fl511_traffic.py         # FL511Client + get_traffic_ahead tool
│   │                            # (also exports _fl511_client, _haversine_distance)
│   ├── evacuation_routing.py    # Imports _fl511_client for road closures
│   ├── road_maintenance.py      # Imports _haversine_distance for dedup
│   ├── speed_advisory.py
│   ├── lane_advisory.py
│   ├── alert_advisory.py
│   ├── base_tool.py
│   ├── registry.py
│   └── __init__.py
└── skills/
    ├── fl511_traffic.py         # LLM prompt for traffic reasoning
    ├── evacuation_routing.py
    ├── road_maintenance.py
    ├── construction_zone.py
    ├── slow_vehicle.py
    ├── base_skill.py
    ├── registry.py
    └── __init__.py
```

## Safety Considerations

- **Advisory-only**: Traffic events produce speed suggestions, never direct actuator commands. All outputs pass through the `SafetyFilter`.
- **Speed clamping**: The safety filter enforces 0–45 m/s (0–100 mph) hard limits and a maximum 20 m/s (~45 mph) sudden reduction.
- **Confidence threshold**: Advisories with confidence below 0.3 are automatically discarded.
- **Alert truncation**: Alert text is capped at 50 characters to fit the HUD.
- **Conservative speed selection**: The longitudinal planner uses `min(targets)` across all sources — if FL511 suggests 35 mph but the construction zone skill suggests 45 mph, the lower speed wins.
- **Stale data handling**: Cached data is used on API failure. The 30-second TTL means data is at most 30 seconds old in normal operation.

## Roadmap

- [ ] Support for FL511 camera snapshots (traffic camera images along the route)
- [ ] Congestion level estimation from event density per road segment
- [ ] Travel time estimation using event data + distance
- [ ] Integration with other state 511 systems (GA511, SC511, AL511) for cross-state trips
- [ ] Historical event pattern analysis (recurring congestion, construction schedules)
- [ ] Waze community report integration for supplementary incident data
