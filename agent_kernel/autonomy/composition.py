"""Dynamic workflow composition from tools and reusable workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Literal, Protocol

from agent_kernel.capabilities import CapabilityRegistry
from agent_kernel.domain.workflow import EdgeSpec, NodeSpec, WorkflowSpec
from agent_kernel.autonomy.workflow_library import WorkflowLibrary


@dataclass(slots=True)
class WorkflowCompositionStep:
  step_id: str
  ref: str
  kind: Literal["tool", "workbench", "workflow"] = "tool"
  input_mapping: dict[str, object] = field(default_factory=dict)
  output_mapping: dict[str, object] = field(default_factory=dict)
  timeout_seconds: int | None = None


@dataclass(slots=True)
class WorkflowCompositionRequest:
  workflow_id: str
  name: str
  steps: list[WorkflowCompositionStep]
  version: str = "0.1.0"
  input_schema: dict[str, object] = field(default_factory=dict)
  output_schema: dict[str, object] = field(default_factory=dict)
  register: bool = False


@dataclass(slots=True)
class WorkflowCompositionResult:
  workflow: WorkflowSpec
  workflow_ref: str | None = None
  warnings: list[str] = field(default_factory=list)


class WorkflowComposer(Protocol):
  def compose(self, request: WorkflowCompositionRequest) -> WorkflowCompositionResult:
    """Compose a workflow from registered capabilities and workflow refs."""


class DeterministicWorkflowComposer:
  """Interface-first deterministic workflow composer.

  The composer validates each step against injected registries, then produces an
  explicit WorkflowSpec. Runtime execution remains delegated to existing node
  executors and CapabilityRuntime policy/audit paths.
  """

  def __init__(
    self,
    capabilities: CapabilityRegistry | None = None,
    workflow_library: WorkflowLibrary | None = None,
  ) -> None:
    self._capabilities = capabilities
    self._workflow_library = workflow_library

  def compose(self, request: WorkflowCompositionRequest) -> WorkflowCompositionResult:
    if not request.steps:
      raise ValueError("Workflow composition requires at least one step.")
    nodes: list[NodeSpec] = []
    edges: list[EdgeSpec] = []
    warnings: list[str] = []
    seen_node_ids: set[str] = set()
    for index, step in enumerate(request.steps):
      node_id = _safe_node_id(step.step_id or f"step_{index + 1}")
      if node_id in seen_node_ids:
        raise ValueError(f"Duplicate composition step_id after normalization: {step.step_id}")
      seen_node_ids.add(node_id)
      nodes.append(self._node_from_step(step, node_id, warnings))
      if index > 0:
        edges.append(EdgeSpec(from_node=nodes[index - 1].node_id, to_node=node_id))
    workflow = WorkflowSpec(
      workflow_id=request.workflow_id,
      version=request.version,
      name=request.name,
      input_schema=dict(request.input_schema),
      output_schema=dict(request.output_schema),
      nodes=nodes,
      edges=edges,
      start_node_id=nodes[0].node_id,
    )
    workflow_ref = None
    if request.register:
      if self._workflow_library is None:
        raise ValueError("workflow_library is required when request.register is true.")
      self._workflow_library.register_workflow(workflow)
      workflow_ref = WorkflowLibrary.workflow_ref(workflow)
    return WorkflowCompositionResult(workflow=workflow, workflow_ref=workflow_ref, warnings=warnings)

  def _node_from_step(
    self,
    step: WorkflowCompositionStep,
    node_id: str,
    warnings: list[str],
  ) -> NodeSpec:
    if step.kind in {"tool", "workbench"}:
      self._validate_capability_step(step, warnings)
    elif step.kind == "workflow":
      self._validate_workflow_step(step, warnings)
    else:
      raise ValueError(f"Unsupported composition step kind: {step.kind}")
    return NodeSpec(
      node_id=node_id,
      kind=step.kind,
      capability_ref=step.ref,
      input_mapping=step.input_mapping,
      output_mapping=step.output_mapping,
      timeout_seconds=step.timeout_seconds,
    )

  def _validate_capability_step(self, step: WorkflowCompositionStep, warnings: list[str]) -> None:
    if self._capabilities is None:
      warnings.append(f"Capability registry not configured; step {step.step_id} was not validated.")
      return
    spec = self._capabilities.get(step.ref)
    if spec.kind != step.kind:
      raise ValueError(f"Capability {step.ref} has kind {spec.kind}, expected {step.kind}.")

  def _validate_workflow_step(self, step: WorkflowCompositionStep, warnings: list[str]) -> None:
    if self._workflow_library is None:
      warnings.append(f"Workflow library not configured; step {step.step_id} was not validated.")
      return
    if self._workflow_library.get_workflow(step.ref) is None:
      raise ValueError(f"Workflow ref not found: {step.ref}")


def _safe_node_id(value: str) -> str:
  normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip()).strip("_")
  if not normalized:
    raise ValueError("Composition step_id must contain at least one alphanumeric character.")
  if normalized[0].isdigit():
    normalized = f"step_{normalized}"
  return normalized
