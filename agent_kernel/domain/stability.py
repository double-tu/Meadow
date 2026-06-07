"""Stability, recovery, budget, and circuit breaker domain models."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from agent_kernel.domain.base import DomainModel, utc_now
from agent_kernel.domain.identifiers import ArtifactRef
from agent_kernel.domain.states import CircuitStatus, RecoveryStatus


@dataclass(slots=True)
class RecoveryJob(DomainModel):
  recovery_id: str
  target_type: Literal["run", "node_step", "tool_call", "agent_session"]
  target_id: str
  status: RecoveryStatus | str
  reason: str
  last_checkpoint_id: str | None = None
  replay_from_event_id: str | None = None
  attempt: int = 0
  created_at: datetime = field(default_factory=utc_now)
  updated_at: datetime = field(default_factory=utc_now)

  def __post_init__(self) -> None:
    if isinstance(self.status, str):
      self.status = RecoveryStatus(self.status)


@dataclass(slots=True)
class DeadLetterItem(DomainModel):
  item_id: str
  target_type: Literal["run", "node_step", "tool_call", "model_call", "agent_session"]
  target_id: str
  reason: str
  failure_type: str
  event_refs: list[str] = field(default_factory=list)
  artifact_refs: list[ArtifactRef] = field(default_factory=list)
  retryable: bool = False
  created_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class RuntimeBudget(DomainModel):
  budget_id: str
  scope: Literal["run", "task", "agent", "agent_pool", "workspace"]
  scope_id: str
  max_wall_time_seconds: int | None = None
  max_cost_usd: float | None = None
  max_model_calls: int | None = None
  max_tool_calls: int | None = None
  max_spawned_agents: int | None = None
  max_tokens: int | None = None
  exhausted_action: Literal["pause", "cancel", "request_approval"] = "pause"


@dataclass(slots=True)
class CircuitBreakerState(DomainModel):
  circuit_id: str
  target_ref: str
  status: CircuitStatus | str
  failure_count: int = 0
  opened_at: datetime | None = None
  next_probe_at: datetime | None = None

  def __post_init__(self) -> None:
    if isinstance(self.status, str):
      self.status = CircuitStatus(self.status)

