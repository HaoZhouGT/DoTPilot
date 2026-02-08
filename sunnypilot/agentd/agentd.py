#!/usr/bin/env python3
"""DoTPilot AI Agent Daemon.

Advisory AI process that observes driving conditions through camera frames
and vehicle telemetry, reasons about the situation using an LLM, and
publishes advisory outputs (speed, lane change, alerts) to the planning pipeline.

The agent is purely advisory and never directly controls actuators.
"""

from cereal import custom
import cereal.messaging as messaging
from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from openpilot.common.realtime import Ratekeeper

from openpilot.sunnypilot.agentd.agent_runner import AgentRunner
from openpilot.sunnypilot.agentd.context_builder import ContextBuilder
from openpilot.sunnypilot.agentd.frame_capture import FrameCapture
from openpilot.sunnypilot.agentd.safety_filter import SafetyFilter

AGENT_RATE = 2  # Hz - main loop rate

AgentState = custom.AgentStateSP.AgentState
AgentBackend = custom.AgentStateSP.Backend
LaneDirection = custom.AgentAdvisorySP.LaneAdvisory.LaneDirection
AlertSeverity = custom.AgentAdvisorySP.AlertAdvisory.Severity

# Maps from internal string/int representation to capnp enums
STATE_MAP = {
  "disabled": AgentState.disabled,
  "initializing": AgentState.initializing,
  "active": AgentState.active,
  "degraded": AgentState.degraded,
  "error": AgentState.error,
}

BACKEND_MAP = {
  "cloud": AgentBackend.cloud,
  "onDevice": AgentBackend.onDevice,
  "none": AgentBackend.none,
}

LANE_DIR_MAP = {
  0: LaneDirection.none,
  1: LaneDirection.left,
  2: LaneDirection.right,
}

SEVERITY_MAP = {
  0: AlertSeverity.info,
  1: AlertSeverity.warning,
  2: AlertSeverity.critical,
}


def main():
  cloudlog.info("agentd: starting")
  params = Params()

  sm = messaging.SubMaster([
    'carState', 'modelV2', 'radarState', 'gpsLocation',
    'liveMapDataSP', 'carStateSP', 'selfdriveState',
    'roadCameraState', 'controlsState',
  ], poll='carState')

  pm = messaging.PubMaster(['agentStateSP', 'agentAdvisorySP'])

  frame_capture = FrameCapture()
  context_builder = ContextBuilder()
  runner = AgentRunner(params)
  safety_filter = SafetyFilter()

  rk = Ratekeeper(AGENT_RATE, print_delay_threshold=0.1)

  while True:
    sm.update()

    # Capture periodic JPEG snapshot (non-blocking, ~1 Hz)
    frame_jpeg = frame_capture.get_snapshot()

    # Build context from all subscribed data
    context = context_builder.build(sm, frame_jpeg)

    # Submit to async LLM runner (returns latest result, non-blocking)
    advisory = runner.get_advisory(context)

    # Safety-filter the advisory before publishing
    advisory = safety_filter.validate(advisory, sm)

    # Publish messages
    _publish_advisory(pm, sm, advisory)
    _publish_state(pm, runner)

    rk.keep_time()


def _publish_advisory(pm: messaging.PubMaster, sm: messaging.SubMaster, advisory):
  msg = messaging.new_message('agentAdvisorySP')
  msg.valid = sm.all_checks(['carState'])
  a = msg.agentAdvisorySP

  if advisory is not None:
    # Speed advisory
    speed = a.speedAdvisory
    speed.active = advisory.speed_active
    speed.speedLimitMs = float(advisory.speed_limit_ms)
    speed.source = advisory.speed_source or ""
    speed.confidence = float(advisory.speed_confidence)
    speed.distanceAheadM = float(advisory.distance_ahead_m)

    # Lane advisory
    lane = a.laneAdvisory
    lane.active = advisory.lane_active
    lane.suggestedDirection = LANE_DIR_MAP.get(advisory.lane_direction, LaneDirection.none)
    lane.reason = advisory.lane_reason or ""
    lane.confidence = float(advisory.lane_confidence)

    # Alert advisory
    alert = a.alertAdvisory
    alert.active = advisory.alert_active
    alert.text = advisory.alert_text or ""
    alert.severity = SEVERITY_MAP.get(advisory.alert_severity, AlertSeverity.info)

    # Meta
    a.activeToolName = advisory.tool_name or ""
    a.advisoryReason = advisory.reason or ""

  pm.send('agentAdvisorySP', msg)


def _publish_state(pm: messaging.PubMaster, runner: AgentRunner):
  msg = messaging.new_message('agentStateSP')
  msg.valid = True
  s = msg.agentStateSP

  s.state = STATE_MAP.get(runner.state, AgentState.disabled)
  s.backend = BACKEND_MAP.get(runner.backend_name, AgentBackend.none)
  s.inferenceLatencyMs = float(runner.last_latency_ms)
  s.sceneSummary = runner.scene_summary or ""
  s.confidence = float(runner.confidence)

  pm.send('agentStateSP', msg)


if __name__ == "__main__":
  main()
