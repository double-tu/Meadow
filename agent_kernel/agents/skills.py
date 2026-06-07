"""Agent skill selection for model context."""

from __future__ import annotations

from typing import Protocol

from agent_kernel.domain.agent import AgentSession
from agent_kernel.domain.skill import SkillCard


class SkillSelector(Protocol):
  def select(self, session: AgentSession, objective: str, limit: int = 5) -> list[SkillCard]:
    ...


class SkillContextProvider:
  """Selects active skills and renders them as compact agent context."""

  def __init__(self, selector: SkillSelector, limit: int = 3) -> None:
    self._selector = selector
    self._limit = limit

  def build_skill_context(
    self,
    session: AgentSession,
    messages: list[dict[str, object]],
  ) -> tuple[list[dict[str, object]], list[SkillCard]]:
    objective = self._objective_from_messages(messages)
    skills = self._selector.select(session, objective, limit=self._limit)
    if not skills:
      return messages, []
    skill_payload = [
      {
        "skill_id": skill.skill_id,
        "name": skill.name,
        "description": skill.description,
        "when_to_use": skill.when_to_use,
        "instructions": skill.instructions,
        "recommended_tools": skill.recommended_tools,
        "recommended_workflows": skill.recommended_workflows,
        "compiled_workflow_ref": skill.compiled_workflow_ref,
      }
      for skill in skills
    ]
    return [
      {
        "role": "system",
        "content": {
          "type": "selected_skills",
          "skills": skill_payload,
        },
      },
      *messages,
    ], skills

  @staticmethod
  def _objective_from_messages(messages: list[dict[str, object]]) -> str:
    parts: list[str] = []
    for message in messages:
      content = message.get("content")
      if isinstance(content, str):
        parts.append(content)
      elif isinstance(content, dict):
        for value in content.values():
          if isinstance(value, str):
            parts.append(value)
    return " ".join(parts)
