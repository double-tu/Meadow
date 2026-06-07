"""Skill card service."""

from __future__ import annotations

from dataclasses import replace

from agent_kernel.domain.base import new_id
from agent_kernel.domain.skill import SkillCard, SkillExecutionMode, SkillStatus
from agent_kernel.domain.workflow import WorkflowSpec


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
  ) -> SkillCard:
    skill = SkillCard(
      skill_id=new_id("skill"),
      name=name,
      description=description,
      when_to_use=when_to_use,
      instructions=instructions,
      execution_mode=SkillExecutionMode.COMPILED_WORKFLOW,
      compiled_workflow_ref=f"workflow://{workflow.workflow_id}/{workflow.version}",
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
    objective_lower = objective.lower()
    candidates = [
      skill
      for skill in self.list_active()
      if objective_lower in skill.when_to_use.lower()
      or objective_lower in skill.description.lower()
      or skill.name.lower() in objective_lower
    ]
    return candidates[:limit]
