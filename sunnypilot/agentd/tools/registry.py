from typing import Type

from openpilot.sunnypilot.agentd.tools.base_tool import BaseTool

_tool_registry: dict[str, Type[BaseTool]] = {}


def register_tool(name: str, description: str):
  """Decorator to register a tool class for the AI agent.

  Usage:
    @register_tool(name="set_speed_advisory", description="...")
    class SpeedAdvisoryTool(BaseTool):
      ...
  """
  def decorator(cls: Type[BaseTool]) -> Type[BaseTool]:
    cls.tool_name = name
    cls.tool_description = description
    _tool_registry[name] = cls
    return cls
  return decorator


def get_all_tools() -> dict[str, Type[BaseTool]]:
  """Return all registered tool classes."""
  return dict(_tool_registry)


def get_tool_schemas() -> list[dict]:
  """Return tool schemas for all registered tools."""
  return [cls.schema() for cls in _tool_registry.values()]


def instantiate_tools() -> dict[str, BaseTool]:
  """Create instances of all registered tools."""
  return {name: cls() for name, cls in _tool_registry.items()}
