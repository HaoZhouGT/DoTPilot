from openpilot.common.swaglog import cloudlog
from openpilot.sunnypilot.agentd.backends.base import BaseLLMBackend


class OnDeviceBackend(BaseLLMBackend):
  """On-device LLM backend stub for future implementation.

  This will run a small local model (e.g., Phi-3, Llama 3B) on the
  Qualcomm Snapdragon for basic pattern recognition when cloud is unavailable.

  Currently returns empty results. Implement when an on-device model is selected.
  """

  def __init__(self):
    self._available = False
    cloudlog.info("agentd: on-device backend initialized (stub)")

  def is_available(self) -> bool:
    return self._available

  def invoke(self, context: dict, tools: list[dict], system_prompt: str) -> dict:
    # Stub: return empty response
    cloudlog.warning("agentd: on-device backend not yet implemented")
    return {
      "content": [{"type": "text", "text": "On-device backend not available."}],
      "stop_reason": "end_turn",
    }
