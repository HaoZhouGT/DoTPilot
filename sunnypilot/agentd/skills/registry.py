from typing import Type

from openpilot.sunnypilot.agentd.skills.base_skill import BaseSkill

_skill_registry: dict[str, Type[BaseSkill]] = {}


def register_skill(name: str, description: str):
  """Decorator to register a skill class for the AI agent.

  Skills provide domain knowledge (via system prompt fragments) that teach the
  LLM when and how to invoke tools for specific driving scenarios.

  Usage:
    @register_skill(name="construction_zone_handler", description="...")
    class ConstructionZoneSkill(BaseSkill):
      ...
  """
  def decorator(cls: Type[BaseSkill]) -> Type[BaseSkill]:
    cls.skill_name = name
    cls.skill_description = description
    _skill_registry[name] = cls
    return cls
  return decorator


def get_all_skills() -> dict[str, Type[BaseSkill]]:
  """Return all registered skill classes."""
  return dict(_skill_registry)


def get_skill_prompt_fragments() -> list[str]:
  """Return system prompt fragments from all registered skills."""
  fragments = []
  for cls in _skill_registry.values():
    instance = cls()
    fragments.append(f"## Skill: {cls.skill_name}\n{instance.get_system_prompt_fragment()}")
  return fragments
