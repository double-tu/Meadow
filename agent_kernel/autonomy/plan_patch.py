"""Plan patch validation."""

from __future__ import annotations

from dataclasses import dataclass, field

from agent_kernel.capabilities import CapabilityRegistry
from agent_kernel.domain.skill import PlanPatch
from agent_kernel.domain.workflow import WorkflowSpec


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
    for index, command in enumerate(patch.commands):
      if command.type == "goto" and not command.target:
        errors.append(f"command[{index}] goto requires target.")
      if command.type == "goto" and workflow is not None and command.target not in workflow_node_ids:
        errors.append(f"command[{index}] goto target does not exist: {command.target}")
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
