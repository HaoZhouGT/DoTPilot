from abc import ABC, abstractmethod


class BaseSkill(ABC):
  skill_name: str = ""
  skill_description: str = ""

  @abstractmethod
  def get_system_prompt_fragment(self) -> str:
    """Return a prompt fragment that teaches the LLM how to use this skill.

    This is injected into the system prompt to give the LLM domain knowledge
    about when and how to invoke tools for this skill's scenario.
    """
    ...
