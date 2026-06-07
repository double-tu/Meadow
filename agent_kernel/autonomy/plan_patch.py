"""Plan patch validation."""

from __future__ import annotations

from dataclasses import dataclass, field

from agent_kernel.capabilities import CapabilityRegistry
from agent_kernel.domain.events import RuntimeEvent, RuntimeEventType
from agent_kernel.domain.skill import PlanPatch
from agent_kernel.domain.workflow import EdgeSpec, NodeSpec, WorkflowSpec


@dataclass(slots=True)
class PlanPatchValidationResult:
  ok: bool
  errors: list[str] = field(default_factory=list)
  warnings: list[str] = field(default_factory=list)


class PlanPatchValidator:
  def __init__(self, capabilities: CapabilityRegistry | None = None) -> None:
    self._capabilities = capabilities

  def validate(self, patch: PlanPatch, workflow: WorkflowSpec | None = None) -> PlanPatchValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    workflow_node_ids = {node.node_id for node in workflow.nodes} if workflow else set()
    future_node_ids = set(workflow_node_ids)
    for index, command in enumerate(patch.commands):
      operation = command.payload.get("operation")
      if command.type == "goto" and not command.target:
        errors.append(f"command[{index}] goto requires target.")
      if command.type == "goto" and workflow is not None and command.target not in workflow_node_ids:
        errors.append(f"command[{index}] goto target does not exist: {command.target}")
      if command.type == "emit_event" and operation == "add_node":
        node_id = command.payload.get("node_id")
        kind = command.payload.get("kind")
        if not isinstance(node_id, str) or not node_id:
          errors.append(f"command[{index}] add_node requires payload.node_id.")
        elif node_id in future_node_ids:
          errors.append(f"command[{index}] add_node duplicates node_id: {node_id}")
        else:
          future_node_ids.add(node_id)
        if not isinstance(kind, str) or not kind:
          errors.append(f"command[{index}] add_node requires payload.kind.")
      if command.type == "emit_event" and operation == "add_edge":
        from_node = command.payload.get("from_node")
        to_node = command.payload.get("to_node")
        if not isinstance(from_node, str) or from_node not in future_node_ids:
          errors.append(f"command[{index}] add_edge from_node does not exist: {from_node}")
        if not isinstance(to_node, str) or to_node not in future_node_ids:
          errors.append(f"command[{index}] add_edge to_node does not exist: {to_node}")
      if command.type == "emit_event" and operation == "set_start":
        node_id = command.payload.get("node_id")
        if not isinstance(node_id, str) or node_id not in future_node_ids:
          errors.append(f"command[{index}] set_start node_id does not exist: {node_id}")
      if command.type in {"spawn_agent", "spawn_task", "send_message"} and not command.payload:
        warnings.append(f"command[{index}] {command.type} has empty payload.")
    if self._capabilities is not None:
      for capability_id in patch.required_capabilities:
        try:
          self._capabilities.get(capability_id)
        except KeyError:
          errors.append(f"required capability not registered: {capability_id}")
    if not patch.commands:
      errors.append("plan patch must contain at least one command.")
    return PlanPatchValidationResult(ok=not errors, errors=errors, warnings=warnings)


@dataclass(slots=True)
class WorkflowPatchResult:
  workflow: WorkflowSpec
  event: RuntimeEvent


class WorkflowPatchApplier:
  """Applies accepted plan patches to workflow specs without mutating the original."""

  def __init__(self, validator: PlanPatchValidator | None = None) -> None:
    self._validator = validator or PlanPatchValidator()

  def apply(self, patch: PlanPatch, workflow: WorkflowSpec) -> WorkflowPatchResult:
    validation = self._validator.validate(patch, workflow)
    if not validation.ok:
      raise ValueError(f"Plan patch is invalid: {validation.errors}")
    nodes = list(workflow.nodes)
    edges = list(workflow.edges)
    start_node_id = workflow.start_node_id
    for index, command in enumerate(patch.commands):
      operation = command.payload.get("operation")
      if command.type == "emit_event" and operation == "add_node":
        nodes.append(self._node_from_payload(command.payload, index))
      elif command.type == "emit_event" and operation == "add_edge":
        edges.append(self._edge_from_payload(command.payload, index))
      elif command.type == "emit_event" and operation == "set_start":
        start_node_id = self._required_str(command.payload, "node_id", index)
      elif command.type in {"continue", "goto", "finish"}:
        continue
      else:
        raise ValueError(f"Unsupported workflow patch command[{index}]: {command.type}/{operation}")
    patched = WorkflowSpec(
      workflow_id=workflow.workflow_id,
      version=self._next_version(workflow.version),
      name=workflow.name,
      input_schema=dict(workflow.input_schema),
      output_schema=dict(workflow.output_schema),
      nodes=nodes,
      edges=edges,
      start_node_id=start_node_id,
    )
    event = RuntimeEvent(
      event_type=RuntimeEventType.PLAN_PATCH_APPLIED,
      run_id=patch.run_id,
      payload={
        "patch_id": patch.patch_id,
        "workflow_id": workflow.workflow_id,
        "from_version": workflow.version,
        "to_version": patched.version,
        "command_count": len(patch.commands),
      },
    )
    return WorkflowPatchResult(workflow=patched, event=event)

  @classmethod
  def _node_from_payload(cls, payload: dict[str, object], index: int) -> NodeSpec:
    return NodeSpec(
      node_id=cls._required_str(payload, "node_id", index),
      kind=cls._required_str(payload, "kind", index),
      capability_ref=cls._optional_str(payload, "capability_ref"),
      input_schema=cls._optional_dict(payload, "input_schema"),
      output_schema=cls._optional_dict(payload, "output_schema"),
      input_mapping=cls._optional_dict(payload, "input_mapping") or {},
      output_mapping=cls._optional_dict(payload, "output_mapping") or {},
      retry_policy=cls._optional_dict(payload, "retry_policy"),
      timeout_seconds=cls._optional_int(payload, "timeout_seconds"),
    )

  @classmethod
  def _edge_from_payload(cls, payload: dict[str, object], index: int) -> EdgeSpec:
    return EdgeSpec(
      from_node=cls._required_str(payload, "from_node", index),
      to_node=cls._required_str(payload, "to_node", index),
      condition=cls._optional_str(payload, "condition"),
    )

  @staticmethod
  def _required_str(payload: dict[str, object], key: str, index: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
      raise ValueError(f"command[{index}] payload.{key} is required.")
    return value

  @staticmethod
  def _optional_str(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None

  @staticmethod
  def _optional_dict(payload: dict[str, object], key: str) -> dict[str, object] | None:
    value = payload.get(key)
    return dict(value) if isinstance(value, dict) else None

  @staticmethod
  def _optional_int(payload: dict[str, object], key: str) -> int | None:
    value = payload.get(key)
    return value if isinstance(value, int) else None

  @staticmethod
  def _next_version(version: str) -> str:
    parts = version.split(".")
    if len(parts) == 3 and all(part.isdigit() for part in parts):
      return f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}"
    return f"{version}+patch"
