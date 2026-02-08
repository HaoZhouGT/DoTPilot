import json
import threading
import time

from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from openpilot.sunnypilot.agentd.backends.cloud_backend import CloudBackend
from openpilot.sunnypilot.agentd.backends.ondevice_backend import OnDeviceBackend
from openpilot.sunnypilot.agentd.tools.base_tool import Advisory, merge_advisories
from openpilot.sunnypilot.agentd.tools.registry import get_tool_schemas, instantiate_tools

# Import tools and skills to trigger registration
import openpilot.sunnypilot.agentd.tools  # noqa: F401
import openpilot.sunnypilot.agentd.skills  # noqa: F401
from openpilot.sunnypilot.agentd.skills.registry import get_skill_prompt_fragments

SYSTEM_PROMPT_BASE = """\
You are an AI driving assistant integrated into DoTPilot, an advanced driver assistance system. \
You observe the road through a forward-facing camera and receive vehicle telemetry data.

Your role is ADVISORY ONLY. You help the driver by:
- Detecting hazards, construction zones, unusual road conditions
- Suggesting speed adjustments for safety
- Suggesting lane changes when beneficial
- Alerting the driver to important situations

You MUST use the provided tools to communicate your observations. \
Do NOT just describe what you see -- take action by calling tools when appropriate. \
If conditions are normal with no hazards, simply respond with a brief text status \
like "Clear road ahead, no action needed."

IMPORTANT SAFETY RULES:
- Never suggest speeds above the posted limit unless already going faster
- Be conservative with confidence scores -- only use 0.8+ when very certain
- Prefer false positives (unnecessary warnings) over false negatives (missed hazards)
- Lane change suggestions are informational only -- the driver must confirm

"""


class AgentRunner:
  """Orchestrates async LLM inference for the AI agent.

  Manages a background inference thread, tool execution, and state tracking.
  The main agentd loop calls get_advisory() each cycle, which is non-blocking:
  it returns the latest available advisory or None.
  """

  def __init__(self, params: Params):
    self.params = params

    # Backends
    self.cloud = CloudBackend(params)
    self.ondevice = OnDeviceBackend()

    # Tools
    self.tool_instances = instantiate_tools()
    self.tool_schemas = get_tool_schemas()

    # Build system prompt with skill fragments
    self._system_prompt = self._build_system_prompt()

    # Async inference state
    self._latest_advisory: Advisory | None = None
    self._lock = threading.Lock()
    self._inference_thread: threading.Thread | None = None

    # Published state
    self.state: str = "initializing"
    self.backend_name: str = "none"
    self.last_latency_ms: float = 0.0
    self.scene_summary: str = ""
    self.confidence: float = 0.0

  def _build_system_prompt(self) -> str:
    prompt = SYSTEM_PROMPT_BASE
    fragments = get_skill_prompt_fragments()
    if fragments:
      prompt += "\n# Domain Knowledge (Skills)\n\n"
      prompt += "\n\n".join(fragments)
    return prompt

  def get_advisory(self, context: dict) -> Advisory | None:
    """Non-blocking: submit context for inference, return latest advisory.

    If no inference is running, starts a new one in a background thread.
    Always returns immediately with the most recent advisory (or None).
    """
    # Start new inference if previous one finished
    if self._inference_thread is None or not self._inference_thread.is_alive():
      self._inference_thread = threading.Thread(
        target=self._run_inference,
        args=(context,),
        daemon=True,
      )
      self._inference_thread.start()

    with self._lock:
      return self._latest_advisory

  def _run_inference(self, context: dict):
    """Run LLM inference in background thread."""
    start_time = time.monotonic()

    try:
      # Select backend
      if self.cloud.is_available():
        backend = self.cloud
        backend_name = "cloud"
      elif self.ondevice.is_available():
        backend = self.ondevice
        backend_name = "onDevice"
      else:
        self._update_state("disabled", "none", 0.0, "No backend available", 0.0)
        return

      # Run inference
      result = backend.invoke(
        context=context,
        tools=self.tool_schemas,
        system_prompt=self._system_prompt,
      )

      latency_ms = (time.monotonic() - start_time) * 1000

      # Process the response
      advisory, summary = self._process_response(result, context)

      with self._lock:
        self._latest_advisory = advisory

      state = "active" if backend_name == "cloud" else "degraded"
      confidence = advisory.speed_confidence if advisory and advisory.speed_active else 0.0
      self._update_state(state, backend_name, latency_ms, summary, confidence)

    except Exception as e:
      latency_ms = (time.monotonic() - start_time) * 1000
      cloudlog.error(f"agentd: inference error: {e}")
      self._update_state("error", "none", latency_ms, f"Error: {e}", 0.0)

  def _process_response(self, result: dict, context: dict) -> tuple[Advisory | None, str]:
    """Parse LLM response and execute any tool calls."""
    content_blocks = result.get("content", [])
    advisories: list[Advisory] = []
    summary = ""

    for block in content_blocks:
      if block.get("type") == "text":
        summary = block.get("text", "")[:200]

      elif block.get("type") == "tool_use":
        tool_name = block.get("name", "")
        tool_input = block.get("input", {})

        tool = self.tool_instances.get(tool_name)
        if tool is None:
          cloudlog.warning(f"agentd: unknown tool called: {tool_name}")
          continue

        try:
          advisory = tool.execute(tool_input, context)
          advisories.append(advisory)
          cloudlog.info(f"agentd: tool {tool_name} executed: {tool_input}")
        except Exception as e:
          cloudlog.error(f"agentd: tool {tool_name} execution error: {e}")

    if advisories:
      return merge_advisories(advisories), summary
    return None, summary

  def _update_state(self, state: str, backend_name: str, latency_ms: float,
                    summary: str, confidence: float):
    self.state = state
    self.backend_name = backend_name
    self.last_latency_ms = latency_ms
    self.scene_summary = summary
    self.confidence = confidence
