![](https://user-images.githubusercontent.com/47793918/233812617-beab2e71-57b9-479e-8bff-c3931347ca40.png)

## What is DoTPilot?

DoTPilot is a fork of [sunnypilot](https://github.com/sunnyhaibin/sunnypilot) (itself a fork of comma.ai's [openpilot](https://github.com/commaai/openpilot)) that integrates an **LLM-based AI agent framework** into the driving assistance pipeline. DoTPilot adds agentic AI intelligence on top of sunnypilot's advanced driver assistance features for 300+ supported vehicles.

The AI agent observes the road through camera imagery and vehicle telemetry, reasons about driving conditions using a large language model, and publishes advisory outputs (speed adjustments, lane change suggestions, driver alerts) through a pluggable **tool/skill framework**.

> **The agent is purely advisory.** It never directly controls steering, throttle, or brakes. All agent outputs flow through the existing safety-validated planning pipeline.

---

## AI Agent Framework

### Architecture Overview

```
                    VisionIPC (road camera)
                           |
                     [JPEG snapshot]
                           |
carState ──────┐           |
modelV2  ──────┤    ┌──────▼──────┐       ┌─────────────┐
radarState ────┤    │             │       │  Cloud LLM   │
gpsLocation ───┼───►│   agentd    │◄─────►│  (Claude /   │
liveMapDataSP ─┤    │             │       │   OpenAI)    │
carStateSP ────┘    │  Tool/Skill │       └─────────────┘
                    │  Framework  │       ┌─────────────┐
                    │             │◄─────►│ On-device    │
                    └──┬───┬───┬──┘       │ fallback LLM │
                       │   │   │          └─────────────┘
            ┌──────────┘   │   └──────────┐
            ▼              ▼              ▼
    agentAdvisorySP  agentStateSP   onroadEventsSP
            │                             │
            ▼                             ▼
    LongitudinalPlannerSP          selfdrived (alerts)
```

**`agentd`** runs as a managed process at 2 Hz. It:

1. Captures periodic JPEG snapshots (640x480, ~1 Hz) from the road camera via VisionIPC
2. Builds a structured driving context from CAN bus data, radar, GPS, and map information
3. Sends the image + context to an LLM with registered tool definitions
4. The LLM reasons about the scene and invokes tools (e.g., `set_speed_advisory`, `set_lane_advisory`, `set_alert`)
5. Tool outputs are merged, safety-filtered, and published as `agentAdvisorySP`
6. The longitudinal planner picks up the advisory as one of its speed target sources

### Key Design Principles

- **Non-blocking**: LLM inference runs in a background thread. The 100 Hz control loop is never affected.
- **Safety-first**: A `SafetyFilter` clamps all outputs (max speed 100 mph, min confidence 0.3, max deceleration 45 mph at once).
- **Conservative integration**: The planner uses `min(targets)` to select the lowest (safest) speed target. The agent can slow the car down but cannot override safety systems to speed it up.
- **Extensible**: New tools and skills are added with simple decorators -- no core code changes needed.

---

## Getting Started

### Prerequisites

- A [comma.ai](https://comma.ai/) device (comma 3/3X) with DoTPilot installed
- An Anthropic API key (for the cloud LLM backend)

### Enable the AI Agent

The agent is controlled by two params:

```bash
# Enable the agent process
echo -n "1" > /data/params/d/AgentEnabled

# Set your Anthropic API key
echo -n "sk-ant-..." > /data/params/d/AgentApiKey
```

Or programmatically:

```python
from openpilot.common.params import Params

params = Params()
params.put_bool("AgentEnabled", True)
params.put("AgentApiKey", "sk-ant-...")
```

When `AgentEnabled` is `true` and the car is onroad, the `agentd` process will start automatically via the process manager.

### Verify It's Running

Subscribe to agent messages to confirm the agent is active:

```python
import cereal.messaging as messaging

sm = messaging.SubMaster(['agentStateSP', 'agentAdvisorySP'])

while True:
    sm.update()

    if sm.updated['agentStateSP']:
        state = sm['agentStateSP']
        print(f"Agent: state={state.state}, backend={state.backend}, "
              f"latency={state.inferenceLatencyMs:.0f}ms")

    if sm.updated['agentAdvisorySP']:
        adv = sm['agentAdvisorySP']
        if adv.speedAdvisory.active:
            speed_mph = adv.speedAdvisory.speedLimitMs * 2.237
            print(f"Speed advisory: {speed_mph:.0f} mph "
                  f"({adv.speedAdvisory.source}, conf={adv.speedAdvisory.confidence:.2f})")
        if adv.laneAdvisory.active:
            print(f"Lane advisory: {adv.laneAdvisory.suggestedDirection} "
                  f"({adv.laneAdvisory.reason})")
        if adv.alertAdvisory.active:
            print(f"Alert: {adv.alertAdvisory.text} [{adv.alertAdvisory.severity}]")
```

---

## Tool & Skill Framework

The agent uses two complementary extension mechanisms:

| Concept | Purpose | How it works |
|---------|---------|--------------|
| **Tool** | An action the LLM can invoke | Defines an input schema (JSON). The LLM calls it via function calling. The tool returns an `Advisory`. |
| **Skill** | Domain knowledge for the LLM | Injects a system prompt fragment that teaches the LLM *when* and *how* to use tools for a specific scenario. |

### Built-in Tools

| Tool | Description |
|------|-------------|
| `set_speed_advisory` | Set a target speed (mph), reason, confidence, and distance ahead |
| `set_lane_advisory` | Suggest a lane change direction (left/right) with reason and confidence |
| `set_alert` | Show a text alert to the driver with severity (info/warning/critical) |

### Built-in Skills

| Skill | Description |
|-------|-------------|
| `construction_zone_handler` | Detects orange cones, construction signs, workers; reduces speed and alerts driver |
| `slow_vehicle_handler` | Detects slow trucks/vehicles ahead; suggests lane changes or speed matching |

---

### Creating a Custom Tool

Create a new file in `sunnypilot/agentd/tools/` and use the `@register_tool` decorator:

```python
# sunnypilot/agentd/tools/weather_advisory.py

from openpilot.sunnypilot.agentd.tools.base_tool import BaseTool, Advisory
from openpilot.sunnypilot.agentd.tools.registry import register_tool


@register_tool(
    name="set_weather_advisory",
    description="Adjust speed for adverse weather conditions such as rain, fog, "
                "snow, or wet roads visible in the camera image."
)
class WeatherAdvisoryTool(BaseTool):

    @classmethod
    def schema(cls) -> dict:
        return {
            "name": "set_weather_advisory",
            "description": cls.tool_description,
            "input_schema": {
                "type": "object",
                "properties": {
                    "condition": {
                        "type": "string",
                        "enum": ["rain", "heavy_rain", "fog", "snow", "wet_road", "ice"],
                        "description": "The detected weather condition.",
                    },
                    "speed_reduction_pct": {
                        "type": "number",
                        "description": "Percentage to reduce speed by (e.g., 20 for 20% reduction).",
                    },
                    "confidence": {
                        "type": "number",
                        "description": "Confidence level from 0.0 to 1.0.",
                    },
                },
                "required": ["condition", "speed_reduction_pct", "confidence"],
            },
        }

    def execute(self, params: dict, context: dict) -> Advisory:
        current_speed_mph = context.get("vehicle", {}).get("speed_mph", 60)
        reduction = params["speed_reduction_pct"] / 100.0
        target_mph = current_speed_mph * (1.0 - reduction)
        target_ms = target_mph * 0.44704

        return Advisory(
            speed_active=True,
            speed_limit_ms=target_ms,
            speed_source=f"weather:{params['condition']}",
            speed_confidence=params["confidence"],
            alert_active=True,
            alert_text=f"{params['condition'].replace('_', ' ').title()} detected",
            alert_severity=1,  # warning
            tool_name="set_weather_advisory",
            reason=params["condition"],
        )
```

Then register it by adding an import in `sunnypilot/agentd/tools/__init__.py`:

```python
from openpilot.sunnypilot.agentd.tools import weather_advisory  # noqa: F401
```

The tool will automatically appear in the LLM's available functions on the next `agentd` restart.

### Creating a Custom Skill

Create a new file in `sunnypilot/agentd/skills/` and use the `@register_skill` decorator:

```python
# sunnypilot/agentd/skills/school_zone.py

from openpilot.sunnypilot.agentd.skills.base_skill import BaseSkill
from openpilot.sunnypilot.agentd.skills.registry import register_skill


@register_skill(
    name="school_zone_handler",
    description="Detects school zones and enforces appropriate speed limits."
)
class SchoolZoneSkill(BaseSkill):

    def get_system_prompt_fragment(self) -> str:
        return (
            "When you detect school zone indicators in the camera image -- such as "
            "'School Zone' signs, flashing yellow beacons, crosswalk markings near "
            "schools, crossing guards, or children near the road -- you should:\n"
            "1. Call set_speed_advisory with speed_mph=25 (or the posted school zone "
            "limit if visible), reason='school_zone', and high confidence (0.8+).\n"
            "2. Call set_alert with text 'School zone - 25 mph' and severity 'warning'.\n"
            "School zone speed limits are typically 15-25 mph and are strictly enforced. "
            "Always err on the side of caution."
        )
```

Register it in `sunnypilot/agentd/skills/__init__.py`:

```python
from openpilot.sunnypilot.agentd.skills import school_zone  # noqa: F401
```

The skill's prompt fragment will automatically be included in the LLM's system prompt.

---

## Message API Reference

The agent communicates through two Cap'n Proto message services on the DoTPilot message bus.

### `agentStateSP` (1 Hz)

Reports agent health and current reasoning state.

| Field | Type | Description |
|-------|------|-------------|
| `state` | enum | `disabled`, `initializing`, `active`, `degraded`, `error` |
| `backend` | enum | `cloud`, `onDevice`, `none` |
| `inferenceLatencyMs` | Float32 | Last LLM inference round-trip time in ms |
| `lastReasoningTimestamp` | UInt64 | Monotonic timestamp (ns) of last reasoning cycle |
| `sceneSummary` | Text | Short text description of what the agent sees |
| `confidence` | Float32 | Overall confidence in current advisory (0-1) |

### `agentAdvisorySP` (2 Hz)

The actionable advisory output consumed by the planning pipeline.

| Field | Type | Description |
|-------|------|-------------|
| `speedAdvisory.active` | Bool | Whether a speed advisory is active |
| `speedAdvisory.speedLimitMs` | Float32 | Target speed in m/s |
| `speedAdvisory.source` | Text | Reason string (e.g., `"construction_zone"`) |
| `speedAdvisory.confidence` | Float32 | Confidence 0-1 |
| `speedAdvisory.distanceAheadM` | Float32 | Distance to condition in meters |
| `laneAdvisory.active` | Bool | Whether a lane change suggestion is active |
| `laneAdvisory.suggestedDirection` | enum | `none`, `left`, `right` |
| `laneAdvisory.reason` | Text | Reason string (e.g., `"slow_truck_ahead"`) |
| `laneAdvisory.confidence` | Float32 | Confidence 0-1 |
| `alertAdvisory.active` | Bool | Whether a driver alert is active |
| `alertAdvisory.text` | Text | Alert text shown on HUD (max 50 chars) |
| `alertAdvisory.severity` | enum | `info`, `warning`, `critical` |
| `activeToolName` | Text | Name(s) of tool(s) that produced this advisory |
| `advisoryReason` | Text | Human-readable combined reason |

### Subscribing to Agent Messages

```python
import cereal.messaging as messaging

sm = messaging.SubMaster(['agentStateSP', 'agentAdvisorySP'])

while True:
    sm.update()

    if sm.updated['agentAdvisorySP']:
        adv = sm['agentAdvisorySP']
        if adv.speedAdvisory.active:
            speed_mph = adv.speedAdvisory.speedLimitMs * 2.237
            print(f"Agent recommends {speed_mph:.0f} mph: {adv.speedAdvisory.source}")
```

---

## File Structure

```
sunnypilot/agentd/
├── __init__.py
├── agentd.py              # Main daemon entry point (2 Hz loop)
├── agent_runner.py        # Async LLM inference orchestration
├── context_builder.py     # Builds driving context from CAN/sensors/map
├── frame_capture.py       # VisionIPC JPEG snapshot capture (~1 Hz)
├── safety_filter.py       # Validates and clamps agent outputs
├── backends/
│   ├── base.py            # Abstract LLM backend interface
│   ├── cloud_backend.py   # Anthropic Messages API (vision + tool use)
│   └── ondevice_backend.py # On-device model fallback (stub)
├── tools/
│   ├── registry.py        # @register_tool decorator and schema generation
│   ├── base_tool.py       # BaseTool ABC, Advisory dataclass, merge logic
│   ├── speed_advisory.py  # set_speed_advisory tool
│   ├── lane_advisory.py   # set_lane_advisory tool
│   └── alert_advisory.py  # set_alert tool
└── skills/
    ├── registry.py        # @register_skill decorator and prompt injection
    ├── base_skill.py      # BaseSkill ABC
    ├── construction_zone.py  # Construction zone detection skill
    └── slow_vehicle.py    # Slow vehicle detection skill
```

### Modified Core Files

| File | Change |
|------|--------|
| `cereal/custom.capnp` | `AgentStateSP`, `AgentAdvisorySP` message structs; `agentAdvisory` plan source |
| `cereal/log.capnp` | Event union members for agent messages |
| `cereal/services.py` | `agentStateSP` (1 Hz), `agentAdvisorySP` (2 Hz) service entries |
| `selfdrive/controls/plannerd.py` | Subscribes to `agentAdvisorySP` |
| `sunnypilot/selfdrive/controls/lib/longitudinal_planner.py` | Consumes agent speed advisory in `update_targets()` |
| `system/manager/process_config.py` | Registers `agentd` process with `AgentEnabled` param gate |

---

## Safety

The AI agent has multiple layers of safety protection:

1. **Advisory only** -- The agent publishes suggestions. It cannot directly command steering, throttle, or brakes.
2. **Safety filter** -- All outputs are clamped before publishing:
   - Maximum speed: 100 mph (45 m/s)
   - Minimum confidence: 0.3 (below this, advisory is discarded)
   - Maximum speed reduction: 45 mph per advisory (prevents sudden extreme braking)
3. **Conservative planner integration** -- The planner selects `min(all_speed_targets)`. The agent can only slow the car, never override other systems to go faster.
4. **Confidence gating** -- The planner only accepts agent advisories with confidence > 0.5.
5. **Process isolation** -- `agentd` runs as a separate process. If it crashes, the driving system continues normally.
6. **Param-gated** -- The agent only runs when explicitly enabled via `AgentEnabled` param.

---

## Configuration

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `AgentEnabled` | bool | `false` | Enable/disable the agent process |
| `AgentApiKey` | string | *(none)* | Anthropic API key for cloud backend |

The cloud backend currently uses `claude-sonnet-4-20250514` with a 10-second timeout. These can be adjusted in `sunnypilot/agentd/backends/cloud_backend.py`.

---

## Roadmap

- [ ] On-device LLM backend (Phi-3 / Llama 3B on Snapdragon)
- [ ] Lane change advisory integration with DesireHelper
- [ ] Agent alerts displayed on the UI HUD
- [ ] Conversation memory (multi-turn reasoning across observations)
- [ ] More skills: school zones, emergency vehicles, weather conditions, highway merging
- [ ] User-configurable skill enable/disable via settings UI
- [ ] Agent performance dashboard (latency, advisory accuracy)

---

## About sunnypilot

[sunnypilot](https://github.com/sunnyhaibin/sunnypilot) is a fork of comma.ai's openpilot, an open source driver assistance system. sunnypilot offers the user a unique driving experience for over 300+ supported car makes and models with modified behaviors of driving assist engagements. sunnypilot complies with comma.ai's safety rules as accurately as possible.

## Running on a dedicated device in a car

First, check out this list of items you'll need to [get started](https://community.sunnypilot.ai/t/getting-started-using-sunnypilot-in-your-supported-car/251).

## Installation

Refer to the sunnypilot community forum for [installation instructions](https://community.sunnypilot.ai/t/read-before-installing-sunnypilot/254), as well as a complete list of [Recommended Branch Installations](https://community.sunnypilot.ai/t/recommended-branch-installations/235).

## Pull Requests

We welcome both pull requests and issues on GitHub. Bug fixes are encouraged.

Pull requests should be against the most current `master` branch.

## User Data

By default, sunnypilot uploads the driving data to comma servers. You can also access your data through [comma connect](https://connect.comma.ai/).

sunnypilot is open source software. The user is free to disable data collection if they wish to do so.

sunnypilot logs the road-facing camera, CAN, GPS, IMU, magnetometer, thermal sensors, crashes, and operating system logs.
The driver-facing camera and microphone are only logged if you explicitly opt-in in settings.

By using this software, you understand that use of this software or its related services will generate certain types of user data, which may be logged and stored at the sole discretion of comma. By accepting this agreement, you grant an irrevocable, perpetual, worldwide right to comma for the use of this data.

## Licensing

sunnypilot is released under the [MIT License](LICENSE). This repository includes original work as well as significant portions of code derived from [openpilot by comma.ai](https://github.com/commaai/openpilot), which is also released under the MIT license with additional disclaimers.

The original openpilot license notice, including comma.ai's indemnification and alpha software disclaimer, is reproduced below as required:

> openpilot is released under the MIT license. Some parts of the software are released under other licenses as specified.
>
> Any user of this software shall indemnify and hold harmless Comma.ai, Inc. and its directors, officers, employees, agents, stockholders, affiliates, subcontractors and customers from and against all allegations, claims, actions, suits, demands, damages, liabilities, obligations, losses, settlements, judgments, costs and expenses (including without limitation attorneys' fees and costs) which arise out of, relate to or result from any use of this software by user.
>
> **THIS IS ALPHA QUALITY SOFTWARE FOR RESEARCH PURPOSES ONLY. THIS IS NOT A PRODUCT.
> YOU ARE RESPONSIBLE FOR COMPLYING WITH LOCAL LAWS AND REGULATIONS.
> NO WARRANTY EXPRESSED OR IMPLIED.**

For full license terms, please see the [`LICENSE`](LICENSE) file.
