"""Skill card service."""

from __future__ import annotations

from dataclasses import replace
import re

from agent_kernel.domain.agent import AgentSession
from agent_kernel.domain.base import new_id
from agent_kernel.domain.skill import SkillCard, SkillExecutionMode, SkillStatus
from agent_kernel.domain.workflow import WorkflowSpec
from agent_kernel.autonomy.workflow_library import WorkflowLibrary


class SkillService:
  def __init__(self, uow_factory) -> None:
    self._uow_factory = uow_factory

  def create_interpreted_skill(
    self,
    name: str,
    description: str,
    when_to_use: str,
    instructions: str,
    recommended_tools: list[str] | None = None,
    recommended_workflows: list[str] | None = None,
  ) -> SkillCard:
    skill = SkillCard(
      skill_id=new_id("skill"),
      name=name,
      description=description,
      when_to_use=when_to_use,
      instructions=instructions,
      execution_mode=SkillExecutionMode.AGENT_INTERPRETED,
      recommended_tools=recommended_tools or [],
      recommended_workflows=recommended_workflows or [],
    )
    self.save(skill)
    return skill

  def create_compiled_workflow_skill(
    self,
    workflow: WorkflowSpec,
    name: str,
    description: str,
    when_to_use: str,
    instructions: str,
    workflow_library: WorkflowLibrary | None = None,
  ) -> SkillCard:
    if workflow_library is not None:
      workflow_library.register_workflow(workflow)
    skill = SkillCard(
      skill_id=new_id("skill"),
      name=name,
      description=description,
      when_to_use=when_to_use,
      instructions=instructions,
      execution_mode=SkillExecutionMode.COMPILED_WORKFLOW,
      compiled_workflow_ref=WorkflowLibrary.workflow_ref(workflow),
      recommended_workflows=[workflow.workflow_id],
    )
    self.save(skill)
    return skill

  def save(self, skill: SkillCard) -> SkillCard:
    with self._uow_factory() as uow:
      uow.autonomy.save_record("skill_card", skill.skill_id, skill)
    return skill

  def activate(self, skill_id: str) -> SkillCard:
    skill = self.get(skill_id)
    if skill is None:
      raise KeyError(f"Skill not found: {skill_id}")
    return self.save(replace(skill, status=SkillStatus.ACTIVE))

  def deprecate(self, skill_id: str) -> SkillCard:
    skill = self.get(skill_id)
    if skill is None:
      raise KeyError(f"Skill not found: {skill_id}")
    return self.save(replace(skill, status=SkillStatus.DEPRECATED))

  def update(self, skill_id: str, data: dict[str, object]) -> SkillCard:
    skill = self.get(skill_id)
    if skill is None:
      raise KeyError(f"Skill not found: {skill_id}")
    updated = replace(
      skill,
      name=str(data.get("name") or skill.name),
      description=str(data.get("description") or skill.description),
      when_to_use=str(data.get("when_to_use") or skill.when_to_use),
      instructions=str(data.get("instructions") or skill.instructions),
      status=SkillStatus(str(data["status"])) if data.get("status") is not None else skill.status,
      recommended_tools=_string_list(data.get("recommended_tools"), fallback=skill.recommended_tools),
      recommended_workflows=_string_list(data.get("recommended_workflows"), fallback=skill.recommended_workflows),
      constraints=_string_list(data.get("constraints"), fallback=skill.constraints),
      failure_modes=_string_list(data.get("failure_modes"), fallback=skill.failure_modes),
    )
    return self.save(updated)

  def get(self, skill_id: str) -> SkillCard | None:
    with self._uow_factory() as uow:
      record = uow.autonomy.get_record("skill_card", skill_id)
    return SkillCard.from_dict(record) if record is not None else None

  def list_all(self) -> list[SkillCard]:
    with self._uow_factory() as uow:
      records = uow.autonomy.list_records("skill_card")
    return [SkillCard.from_dict(record) for record in records]

  def list_active(self) -> list[SkillCard]:
    return [skill for skill in self.list_all() if skill.status is SkillStatus.ACTIVE]

  def select_for_prompt(self, objective: str, limit: int = 5) -> list[SkillCard]:
    scored = [
      (self._score_skill(skill, objective), skill)
      for skill in self.list_active()
    ]
    return [skill for score, skill in sorted(scored, key=lambda item: item[0], reverse=True) if score > 0][:limit]

  def select(self, session: AgentSession, objective: str, limit: int = 5) -> list[SkillCard]:
    allowed: list[SkillCard] = []
    for skill in self.select_for_prompt(objective, limit=limit * 2):
      policy = skill.use_policy
      if policy is not None:
        if policy.allowed_agent_ids and session.agent_id not in policy.allowed_agent_ids:
          continue
      allowed.append(skill)
      if len(allowed) >= limit:
        break
    return allowed

  @classmethod
  def _score_skill(cls, skill: SkillCard, objective: str) -> int:
    objective_tokens = cls._tokens(objective)
    if not objective_tokens:
      return 0
    searchable = " ".join([skill.name, skill.description, skill.when_to_use, *skill.recommended_tools])
    skill_tokens = cls._tokens(searchable)
    score = len(objective_tokens & skill_tokens)
    objective_lower = objective.lower()
    if skill.name.lower() in objective_lower:
      score += 3
    if skill.when_to_use.lower() and skill.when_to_use.lower() in objective_lower:
      score += 2
    return score

  @staticmethod
  def _tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-zA-Z0-9_]+", text.lower()) if len(token) > 2}


def _string_list(value: object, *, fallback: list[str]) -> list[str]:
  if value is None:
    return fallback
  if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
    raise ValueError("Expected a list of strings.")
  return value
