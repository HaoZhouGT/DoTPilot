# Evacuation Routing Tool

The Evacuation Routing Tool provides real-time emergency intelligence for drivers in Florida. It aggregates data from five public sources — weather alerts, evacuation zones, emergency shelters, road closures, and route planning — into a single tool that the DoTPilot AI agent can invoke to help drivers navigate hurricane evacuations, flood events, and other emergencies.

## What It Does

When a weather emergency threatens Florida, the tool answers three critical questions:

1. **Am I in danger?** (`check_situation`) — Queries NWS for active hurricane/flood/storm surge warnings, determines your FDEM evacuation zone (A–F), and checks FL511 for road closures. Returns a comprehensive situation report.

2. **How do I get to safety?** (`plan_route`) — Finds the nearest emergency shelter, checks for road closures along the route corridor, and provides turn-by-turn driving directions via OSRM.

3. **Where can I go?** (`find_shelters`) — Locates nearby emergency shelters with capacity, availability, pet-friendly status, and special needs accommodations.

The AI agent uses this data to issue speed advisories, driver alerts, and lane change suggestions through the standard DoTPilot advisory pipeline.

## Architecture

```
                    ┌──────────────────────────────────────────┐
                    │          plan_evacuation tool             │
                    ├──────────────────────────────────────────┤
                    │                                          │
  GPS location ───► │  ┌──────────┐  ┌──────────┐             │
                    │  │NWSClient │  │FDEMClient│             │
  Vehicle state ──► │  │          │  │          │             │
                    │  │ Weather  │  │ Zones    │             │
                    │  │ alerts   │  │ Routes   │             │
                    │  │          │  │ Shelters │             │
                    │  └────┬─────┘  └────┬─────┘             │
                    │       │             │                    │
                    │  ┌────┴─────┐  ┌────┴─────┐             │
                    │  │FL511     │  │OSRM      │             │
                    │  │Client    │  │Client    │             │
                    │  │(reused)  │  │          │             │
                    │  │ Road     │  │ Route    │             │
                    │  │ closures │  │ planning │             │
                    │  └────┬─────┘  └────┬─────┘             │
                    │       │             │                    │
                    │       └──────┬──────┘                    │
                    │              ▼                            │
                    │    Advisory (alert + speed + reason)      │
                    └──────────────────┬───────────────────────┘
                                       │
                                       ▼
                              LLM reasoning layer
                                       │
                               ┌───────┼───────┐
                               ▼       ▼       ▼
                            Speed    Alert    Lane
                           advisory advisory advisory
```

## Data Sources

### NWS Weather Alerts (National Weather Service)

- **Endpoint**: `https://api.weather.gov/alerts/active?point={lat},{lon}`
- **Authentication**: None (requires `User-Agent` header)
- **Rate limit**: 1 request per 30 seconds
- **Cache TTL**: 60 seconds
- **Data returned**: Active weather alerts with event type, severity, urgency, headline, description, and instructions

The tool filters for evacuation-relevant events:

| Event | Typical Trigger |
|-------|----------------|
| Hurricane Warning | Hurricane expected within 36 hours |
| Storm Surge Warning | Life-threatening storm surge expected |
| Flood Warning | Flooding imminent or occurring |
| Flash Flood Warning | Flash flooding imminent |
| Tropical Storm Warning | Tropical storm expected within 36 hours |
| Tornado Warning | Tornado detected or imminent |
| Extreme Wind Warning | Sustained 115+ mph winds expected |
| Coastal Flood Warning | Coastal flooding expected |

### FDEM Evacuation Zones (Florida Division of Emergency Management)

- **Endpoint**: ArcGIS REST service at `services.arcgis.com/3wFbqsFPLeKqOlIK/arcgis/rest/services/`
- **Authentication**: None (public government data)
- **Cache TTL**: 300 seconds (zone data is static)
- **Data returned**: Evacuation zone classification, county name, evacuation status

Florida evacuation zones indicate storm surge vulnerability:

| Zone | Priority | Description |
|------|----------|-------------|
| **A** | 1 (highest) | Barrier islands, coastal areas, mobile homes. Evacuate first. |
| **B** | 2 | Low-lying areas near coast |
| **C** | 3 | Further inland, at risk in strong storms |
| **D** | 4 | Inland areas, at risk in major hurricanes |
| **E** | 5 | Typically only Cat 4-5 hurricanes |
| **F** | 6 (lowest) | Least vulnerable zone |

### FDEM Emergency Shelters

Two shelter layers are queried:

1. **Open Shelters** (`Open_Shelters_in_Florida_(View_Only)`) — Populated during active events with real-time capacity and occupancy data.
2. **Shelter Inventory** (`Risk_Shelter_Inventory_General`) — Pre-planned shelters with capacity, type, and evacuation zone information. Available year-round.

Shelter data includes: name, address, county, capacity, occupancy, availability, pet-friendly flag, special needs flag, and GPS coordinates.

### FDEM Evacuation Routes

- **Endpoint**: `Evacuation_Routes_Hosted/FeatureServer/0`
- **Data returned**: Designated evacuation route names, highway numbers, shield types, and counties
- Used to inform drivers which roads are official evacuation corridors

### FL511 Traffic Events (Reused)

The tool imports the existing `FL511Client` singleton from the FL511 Traffic Tool. This shares the cache and respects the FL511 rate limit (10 calls per 60 seconds). Used to check for road closures and accidents along evacuation corridors.

### OSRM Routing (Open Source Routing Machine)

- **Endpoint**: `https://router.project-osrm.org/route/v1/driving/{coords}`
- **Authentication**: None (public demo server)
- **Cache TTL**: 60 seconds
- **Data returned**: Total distance, duration, and step-by-step turn-by-turn directions

For production use, consider self-hosting an OSRM instance or switching to the Google Maps Routes API for more reliable service under load.

## How It Was Built

### Design Principles

1. **Multi-source aggregation**: No single data source tells the full evacuation story. The tool combines weather intelligence, geographic zone data, real-time traffic conditions, and route planning into a unified assessment.

2. **Aggressive caching**: Evacuation situations evolve over hours, not seconds. NWS alerts are cached for 60s, FDEM data for 300s, OSRM routes for 60s. This respects rate limits and keeps the tool responsive.

3. **Graceful degradation**: Every API call is wrapped in try/except. If NWS is down, the tool falls back to FL511 data. If FDEM is unreachable, the zone check is skipped. If OSRM fails, shelter coordinates are provided without routing. The driving system never crashes due to an API failure.

4. **Reuse over duplication**: The FL511 client singleton is imported directly from the FL511 Traffic Tool rather than creating a second client instance. This ensures shared caching and rate limit compliance.

5. **Advisory-only output**: The tool produces `Advisory` objects that flow through the safety filter before reaching the planner. Hard limits on speed (0–100 mph), confidence thresholds (>0.3), and maximum deceleration rates are enforced regardless of tool output.

### Implementation Pattern

The tool follows the DoTPilot agent tool framework:

```python
@register_tool(name="plan_evacuation", description="...")
class EvacuationRoutingTool(BaseTool):

    @classmethod
    def schema(cls) -> dict:
        # Anthropic tool_use schema with 3 actions
        ...

    def execute(self, params: dict, context: dict) -> Advisory:
        # Dispatch to action handler based on params["action"]
        ...
```

Module-level singleton clients handle caching and API communication:

```python
_nws_client = NWSClient()      # Weather alerts
_fdem_client = FDEMClient()    # Zones, routes, shelters
_osrm_client = OSRMClient()    # Route planning
```

The companion skill (`EvacuationRoutingSkill`) provides a system prompt fragment that teaches the LLM:
- When to invoke the tool (hurricane season, visual flooding, heavy evacuation traffic)
- The two-phase assessment protocol (check situation first, then plan route)
- How to interpret zone priorities and alert severity levels
- How to cross-reference multiple data sources for confidence scoring
- Speed and lane advisory guidelines during evacuation

### Coordinate Systems

FDEM shelter data uses Web Mercator (EPSG:3857) projection while the tool expects WGS84 (EPSG:4326) GPS coordinates. The `FDEMClient` includes a `_web_mercator_to_wgs84()` conversion that handles this transparently.

Evacuation zones and routes are returned in WGS84 natively, so no conversion is needed for those layers.

### Haversine Distance

All distance calculations use the haversine formula (shared from `fl511_traffic.py`) for GPS-accurate great-circle distances. This is used for:
- Filtering shelters within the search radius
- Estimating route corridor width for FL511 closure checks
- Sorting results by distance from the vehicle

## Configuration

| Parameter | Description | Required |
|-----------|-------------|----------|
| `AgentEnabled` | Enable the AI agent daemon | Yes |
| `AgentApiKey` | Anthropic API key for LLM backend | Yes |
| `FL511ApiKey` | FL511 API key for road closure data | Optional (enhances route corridor checks) |

NWS, FDEM, and OSRM require no API keys.

To get an FL511 API key, register at [fl511.com/developers](https://fl511.com/developers).

## Tool Schema

The LLM invokes the tool with the following parameters:

```json
{
  "name": "plan_evacuation",
  "input": {
    "action": "check_situation | plan_route | find_shelters",
    "destination_lat": 28.5383,
    "destination_lon": -81.3792,
    "radius_miles": 25
  }
}
```

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `action` | string | Yes | — | `check_situation`, `plan_route`, or `find_shelters` |
| `destination_lat` | number | No | Nearest shelter | Latitude for route destination |
| `destination_lon` | number | No | Nearest shelter | Longitude for route destination |
| `radius_miles` | number | No | 50 (check) / 25 (shelters) | Search radius |

## Example Outputs

### check_situation (Hurricane Warning active, Zone A)

```
Alert: "EVACUATE: Zone A active" (severity: critical)

Reason:
⚠ ACTIVE WEATHER ALERTS:
  - Hurricane Warning (Extreme): Hurricane Milton — Life-threatening
    storm surge and hurricane conditions expected.
    Instructions: Evacuate immediately if in Zone A or B.

Evacuation Zone: A (Miami-Dade County). Status: Mandatory Evacuation
  ** Zone A is high-priority for evacuation **

Nearby designated evacuation routes: I-95, Florida Turnpike, US-1

⚠ 2 road closure(s) nearby:
  - I-95 Southbound: Bridge closure due to high winds (3.2mi away)
  - US-1 Northbound: Flooding reported (5.1mi away)
```

### plan_route (to nearest shelter)

```
Alert: "Evac route: 23.4 mi, ~35 min" (severity: critical)
Speed: 55 mph (confidence: 0.7, source: evacuation_routing)

Reason:
Routing to nearest shelter: Miami Springs Recreation Center
  (200 Westward Dr Miami Springs 33166, 8.2mi away, capacity: 500)

⚠ 1 closure(s)/incident(s) on route corridor:
  - I-95 Southbound: Bridge closure due to high winds

Route: 23.4 mi, ~35 min
Directions:
  1. Head north on NW 2nd Ave (0.3 mi)
  2. Turn right onto NW 36th St (1.2 mi)
  3. Merge onto I-95 Northbound (8.4 mi)
  4. Take exit onto NW 79th St (0.5 mi)
  ...

Designated evacuation routes nearby: I-95, Florida Turnpike
```

### find_shelters

```
Alert: "8 shelter(s) within 25mi" (severity: info)

Reason:
Found 8 shelter(s) within 25 miles:

  1. Miami Springs Recreation Center [Open] — 8.2mi
     200 Westward Dr Miami Springs 33166, Miami-Dade County
     Capacity: 500, 312 spots available (Pet-friendly)
     GPS: 25.8225, -80.2898

  2. Hialeah Gardens Community Center — 10.1mi
     11000 NW 87th Ct Hialeah Gardens 33018, Miami-Dade County
     Capacity: 350 (Special needs)
     GPS: 25.8870, -80.3486
  ...
```

## File Structure

```
sunnypilot/agentd/
├── tools/
│   ├── evacuation_routing.py    # Tool: NWSClient, FDEMClient, OSRMClient,
│   │                            #       EvacuationRoutingTool
│   ├── fl511_traffic.py         # Tool: FL511Client (reused by evacuation tool)
│   ├── speed_advisory.py        # Tool: set_speed_advisory
│   ├── lane_advisory.py         # Tool: set_lane_advisory
│   ├── alert_advisory.py        # Tool: set_alert
│   ├── base_tool.py             # Advisory dataclass, BaseTool ABC
│   ├── registry.py              # @register_tool decorator
│   └── __init__.py              # Auto-imports for registration
└── skills/
    ├── evacuation_routing.py    # Skill: LLM prompt for evacuation reasoning
    ├── fl511_traffic.py         # Skill: LLM prompt for traffic reasoning
    ├── construction_zone.py     # Skill: Construction zone detection
    ├── slow_vehicle.py          # Skill: Slow vehicle detection
    ├── base_skill.py            # BaseSkill ABC
    ├── registry.py              # @register_skill decorator
    └── __init__.py              # Auto-imports for registration
```

## Safety Considerations

- **Advisory-only**: The tool never directly controls the vehicle. All outputs pass through the `SafetyFilter` before reaching the longitudinal planner.
- **Speed clamping**: The safety filter enforces 0–45 m/s (0–100 mph) hard limits and maximum deceleration of 20 m/s (~45 mph reduction).
- **Confidence threshold**: Advisories with confidence below 0.3 are automatically discarded.
- **Alert truncation**: Alert text is truncated to 50 characters to fit the HUD display.
- **No direct actuator access**: The tool output flows through `agentAdvisorySP → LongitudinalPlannerSP → min(targets)`, which always selects the safest (lowest) speed from all sources.

## Roadmap

- [ ] Self-hosted OSRM instance for production reliability
- [ ] Contra-flow lane detection from camera feed
- [ ] Multi-stop evacuation planning (pickup family members)
- [ ] Integration with Waze community reports
- [ ] Push notifications for zone status changes
- [ ] Historical evacuation traffic pattern analysis
- [ ] Support for other 511 systems (GA511, SC511) for cross-state evacuation
