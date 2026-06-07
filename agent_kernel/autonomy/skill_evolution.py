"""Skill evolution records."""

from agent_kernel.domain.autonomy import GoldenTrace, SkillEvolutionRecord, WorkflowTemplate
from agent_kernel.domain.base import new_id


class SkillEvolutionService:
  def __init__(self, uow_factory) -> None:
    self._uow_factory = uow_factory

  def record_compiled_workflow(
    self,
    trace: GoldenTrace,
    template: WorkflowTemplate,
    rationale: str,
  ) -> SkillEvolutionRecord:
    record = SkillEvolutionRecord(
      record_id=new_id("skill_evolution"),
      source_trace_id=trace.trace_id,
      workflow_template_id=template.template_id,
      decision="compile_workflow",
      rationale=rationale,
    )
    with self._uow_factory() as uow:
      uow.autonomy.save_skill_evolution(record)
    return record

