"""Node step domain model."""

from dataclasses import dataclass, field
from datetime import datetime

from agent_kernel.domain.base import DomainModel, utc_now
from agent_kernel.domain.states import NodeStepStatus


@dataclass(slots=True)
class NodeStepRecord(DomainModel):
  step_id: str
  run_id: str
  node_id: str
  status: NodeStepStatus | str
  attempt: int = 0
  idempotency_key: str | None = None
  lease_id: str | None = None
  error: str | None = None
  created_at: datetime = field(default_factory=utc_now)
  updated_at: datetime = field(default_factory=utc_now)

  def __post_init__(self) -> None:
    if isinstance(self.status, str):
      self.status = NodeStepStatus(self.status)

