from abc import ABC, abstractmethod


class BaseLLMBackend(ABC):
  """Abstract interface for LLM backends."""

  @abstractmethod
  def invoke(self, context: dict, tools: list[dict], system_prompt: str) -> dict:
    """Run LLM inference with the given context and tool definitions.

    Args:
      context: Structured driving context (vehicle state, leads, frame, etc.)
      tools: List of tool schemas in internal format.
      system_prompt: System prompt including skill fragments.

    Returns:
      Raw LLM response dict containing text and/or tool_use blocks.
    """
    ...

  @abstractmethod
  def is_available(self) -> bool:
    """Check if this backend is currently available for inference."""
    ...
