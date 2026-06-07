"""Runtime event model."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from agent_kernel.domain.base import DomainModel, new_id, utc_now
from agent_kernel.domain.errors import DomainValidationError
from agent_kernel.domain.identifiers import ArtifactRef
from agent_kernel.domain.serialization import to_json

MAX_EVENT_PAYLOAD_BYTES = 8 * 1024


class RuntimeEventType(StrEnum):
  RUN_CREATED = "run.created"
  RUN_STARTED = "run.started"
  RUN_PAUSED = "run.paused"
  RUN_RESUMED = "run.resumed"
  RUN_COMPLETED = "run.completed"
  RUN_FAILED = "run.failed"
  RUN_CANCELLED = "run.cancelled"
  STEP_STARTED = "step.started"
  STEP_COMPLETED = "step.completed"
  STEP_FAILED = "step.failed"
  MODEL_CALL_STARTED = "model.call.started"
  MODEL_CALL_COMPLETED = "model.call.completed"
  TOOL_CALL_STARTED = "tool.call.started"
  TOOL_CALL_COMPLETED = "tool.call.completed"
  TOOL_CALL_CANCEL_REQUESTED = "tool.call.cancel_requested"
  TOOL_CALL_KILL_REQUESTED = "tool.call.kill_requested"
  TOOL_CALL_CANCELLED = "tool.call.cancelled"
  TOOL_CALL_KILLED = "tool.call.killed"
  TOOL_CALL_FAILED = "tool.call.failed"
  MEMORY_READ = "memory.read"
  MEMORY_WRITE = "memory.write"
  MEMORY_EVOLUTION_CANDIDATE = "memory.evolution_candidate"
  CONTEXT_BUILT = "context.built"
  APPROVAL_REQUESTED = "approval.requested"
  APPROVAL_RESOLVED = "approval.resolved"
  ARTIFACT_CREATED = "artifact.created"
  HUMAN_INTERVENTION = "human.intervention"
  PLAN_PATCH_APPLIED = "plan_patch.applied"
  RECOVERY_SUCCEEDED = "recovery.succeeded"
  RECOVERY_FAILED = "recovery.failed"
  AGENT_SESSION_CREATED = "agent.session.created"
  AGENT_TURN_COMPLETED = "agent.turn.completed"
  AGENT_SESSION_CANCELLED = "agent.session.cancelled"
  AGENT_DELEGATION_STARTED = "agent.delegation.started"
  AGENT_DELEGATION_COMPLETED = "agent.delegation.completed"
  AGENT_DELEGATION_FAILED = "agent.delegation.failed"
  AGENT_DELEGATION_CANCELLED = "agent.delegation.cancelled"


@dataclass(slots=True)
class RuntimeEvent(DomainModel):
  event_type: RuntimeEventType | str
  run_id: str
  event_id: str = field(default_factory=lambda: new_id("evt"))
  timestamp: datetime = field(default_factory=utc_now)
  node_id: str | None = None
  step_id: str | None = None
  agent_id: str | None = None
  task_id: str | None = None
  causal_id: str | None = None
  payload: dict[str, Any] = field(default_factory=dict)
  artifact_refs: list[ArtifactRef] = field(default_factory=list)

  def __post_init__(self) -> None:
    if not self.run_id:
      raise DomainValidationError("RuntimeEvent.run_id is required.")
    if isinstance(self.event_type, str):
      self.event_type = RuntimeEventType(self.event_type)
    payload_size = len(to_json(self.payload).encode("utf-8"))
    if payload_size > MAX_EVENT_PAYLOAD_BYTES:
      raise DomainValidationError(
        "RuntimeEvent.payload is too large; store large content as an artifact ref."
      )
