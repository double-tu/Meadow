"""Workflow domain models."""

from dataclasses import dataclass, field
from typing import Any, Literal

from agent_kernel.domain.base import DomainModel
from agent_kernel.domain.errors import DomainValidationError
from agent_kernel.domain.events import RuntimeEvent
from agent_kernel.domain.identifiers import ArtifactRef


@dataclass(slots=True)
class NodeSpec(DomainModel):
  node_id: str
  kind: str
  capability_ref: str | None = None
  input_schema: dict[str, Any] | None = None
  output_schema: dict[str, Any] | None = None
  input_mapping: dict[str, Any] = field(default_factory=dict)
  output_mapping: dict[str, Any] = field(default_factory=dict)
  retry_policy: dict[str, Any] | None = None
  timeout_seconds: int | None = None


@dataclass(slots=True)
class EdgeSpec(DomainModel):
  from_node: str
  to_node: str
  condition: str | None = None


@dataclass(slots=True)
class WorkflowSpec(DomainModel):
  workflow_id: str
  version: str
  name: str
  input_schema: dict[str, Any]
  output_schema: dict[str, Any]
  nodes: list[NodeSpec]
  edges: list[EdgeSpec]
  start_node_id: str

  def __post_init__(self) -> None:
    node_ids = [node.node_id for node in self.nodes]
    unique_node_ids = set(node_ids)
    if len(node_ids) != len(unique_node_ids):
      raise DomainValidationError("WorkflowSpec.nodes contains duplicate node_id values.")
    if self.start_node_id not in unique_node_ids:
      raise DomainValidationError("WorkflowSpec.start_node_id must reference an existing node.")
    for edge in self.edges:
      if edge.from_node not in unique_node_ids:
        raise DomainValidationError(f"Edge from_node does not exist: {edge.from_node}")
      if edge.to_node not in unique_node_ids:
        raise DomainValidationError(f"Edge to_node does not exist: {edge.to_node}")


@dataclass(slots=True)
class ExecutionCommand(DomainModel):
  type: Literal[
    "continue",
    "goto",
    "branch",
    "spawn_task",
    "spawn_agent",
    "await_task",
    "send_message",
    "emit_event",
    "interrupt",
    "request_approval",
    "retry",
    "compensate",
    "finish",
    "fail",
  ]
  target: str | None = None
  payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class NodeContext(DomainModel):
  run_id: str
  step_id: str
  node: NodeSpec
  state: dict[str, Any]
  input: dict[str, Any]
  idempotency_key: str
  grants: list[str] = field(default_factory=list)


@dataclass(slots=True)
class NodeResult(DomainModel):
  state_patch: dict[str, Any] = field(default_factory=dict)
  command: ExecutionCommand | None = None
  events: list[RuntimeEvent] = field(default_factory=list)
  artifact_refs: list[ArtifactRef] = field(default_factory=list)

