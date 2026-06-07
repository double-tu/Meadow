"""Policy, approval, and runtime steering domain models."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from agent_kernel.domain.base import DomainModel, utc_now
from agent_kernel.domain.identifiers import ArtifactRef
from agent_kernel.domain.states import ApprovalStatus


class InterventionType(StrEnum):
  CORRECTION = "correction"
  CONSTRAINT_UPDATE = "constraint_update"
  ENVIRONMENT_UPDATE = "environment_update"
  PRIORITY_CHANGE = "priority_change"
  STOP_INSTRUCTION = "stop_instruction"
  APPROVAL_HINT = "approval_hint"


class InterventionApplyMode(StrEnum):
  CONTINUE_NEXT_TURN = "continue_next_turn"
  PAUSE_AND_RESUME = "pause_and_resume"
  CANCEL_CURRENT_STEP_AND_RESUME = "cancel_current_step_and_resume"


@dataclass(slots=True)
class ApprovalRequest(DomainModel):
  approval_id: str
  run_id: str
  target_type: Literal["run", "node_step", "tool_call", "agent_session"]
  target_id: str
  reason: str
  status: ApprovalStatus | str = ApprovalStatus.REQUESTED
  requested_payload: dict[str, Any] = field(default_factory=dict)
  created_at: datetime = field(default_factory=utc_now)
  resolved_at: datetime | None = None

  def __post_init__(self) -> None:
    if isinstance(self.status, str):
      self.status = ApprovalStatus(self.status)


@dataclass(slots=True)
class HumanIntervention(DomainModel):
  intervention_id: str
  run_id: str
  type: InterventionType | str
  content: str
  apply_mode: InterventionApplyMode | str
  thread_id: str | None = None
  task_id: str | None = None
  priority: Literal["normal", "high", "critical"] = "normal"
  artifact_refs: list[ArtifactRef] = field(default_factory=list)
  created_at: datetime = field(default_factory=utc_now)

  def __post_init__(self) -> None:
    if isinstance(self.type, str):
      self.type = InterventionType(self.type)
    if isinstance(self.apply_mode, str):
      self.apply_mode = InterventionApplyMode(self.apply_mode)


@dataclass(slots=True)
class RuntimeControlRequest(DomainModel):
  control_id: str
  run_id: str
  target_type: Literal["run", "node_step", "tool_call", "agent_session", "group_chat"]
  target_id: str
  action: Literal["pause", "resume", "cancel", "kill", "inject_intervention"]
  reason: str
  intervention: HumanIntervention | None = None
  created_at: datetime = field(default_factory=utc_now)

