"""Workflow template library."""

from agent_kernel.domain.autonomy import GoldenTrace, WorkflowTemplate, WorkflowTemplateStatus
from agent_kernel.domain.base import new_id
from agent_kernel.domain.workflow import WorkflowSpec
from agent_kernel.workflow.graph import WorkflowGraph


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

  def register_workflow(self, workflow: WorkflowSpec) -> WorkflowSpec:
    graph = WorkflowGraph(workflow)
    for node in workflow.nodes:
      graph.next_node_id(node.node_id, {})
    with self._uow_factory() as uow:
      uow.autonomy.save_workflow_spec(workflow)
    return workflow

  def get_workflow(self, workflow_ref: str) -> WorkflowSpec | None:
    workflow_id, version = self._parse_workflow_ref(workflow_ref)
    with self._uow_factory() as uow:
      return uow.autonomy.get_workflow_spec(workflow_id, version)

  def list_workflows(self, workflow_id: str | None = None) -> list[WorkflowSpec]:
    with self._uow_factory() as uow:
      return uow.autonomy.list_workflow_specs(workflow_id)

  @staticmethod
  def workflow_ref(workflow: WorkflowSpec) -> str:
    return f"workflow://{workflow.workflow_id}/{workflow.version}"

  @staticmethod
  def _parse_workflow_ref(workflow_ref: str) -> tuple[str, str]:
    prefix = "workflow://"
    if not workflow_ref.startswith(prefix):
      raise ValueError(f"Invalid workflow ref: {workflow_ref}")
    rest = workflow_ref.removeprefix(prefix)
    workflow_id, _, version = rest.rpartition("/")
    if not workflow_id or not version:
      raise ValueError(f"Invalid workflow ref: {workflow_ref}")
    return workflow_id, version
