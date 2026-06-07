"""Workflow template library."""

from agent_kernel.domain.autonomy import GoldenTrace, WorkflowTemplate, WorkflowTemplateStatus
from agent_kernel.domain.base import new_id


class WorkflowLibrary:
  def __init__(self, uow_factory) -> None:
    self._uow_factory = uow_factory

  def publish_draft(
    self,
    trace: GoldenTrace,
    name: str,
    workflow_spec_ref: str,
    applicability: str,
  ) -> WorkflowTemplate:
    template = WorkflowTemplate(
      template_id=new_id("workflow_template"),
      name=name,
      status=WorkflowTemplateStatus.DRAFT,
      workflow_spec_ref=workflow_spec_ref,
      source_trace_id=trace.trace_id,
      applicability=applicability,
    )
    with self._uow_factory() as uow:
      uow.autonomy.save_workflow_template(template)
    return template

  def list_templates(self) -> list[WorkflowTemplate]:
    with self._uow_factory() as uow:
      return uow.autonomy.list_workflow_templates()

