"""Task domain models."""

from dataclasses import dataclass, field
from datetime import datetime

from agent_kernel.domain.base import DomainModel, utc_now
from agent_kernel.domain.identifiers import ArtifactRef
from agent_kernel.domain.states import TaskStatus


@dataclass(slots=True)
class Task(DomainModel):
  task_id: str
  objective_id: str
  title: str
  status: TaskStatus | str
  owner_agent_id: str | None = None
  depends_on: list[str] = field(default_factory=list)
  blocked_reason: str | None = None
  result_artifact_refs: list[ArtifactRef] = field(default_factory=list)
  created_at: datetime = field(default_factory=utc_now)
  updated_at: datetime = field(default_factory=utc_now)

  def __post_init__(self) -> None:
    if isinstance(self.status, str):
      self.status = TaskStatus(self.status)


@dataclass(slots=True)
class TaskClaim(DomainModel):
  claim_id: str
  task_id: str
  claimant_id: str
  lease_id: str
  heartbeat_at: datetime
  expires_at: datetime

