"""Run aggregate view."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agent_kernel.domain.base import DomainModel, utc_now
from agent_kernel.domain.identifiers import ArtifactRef, EntityRef, MemoryRef
from agent_kernel.domain.states import RunStatus


@dataclass(slots=True)
class RunState(DomainModel):
  run_id: str
  status: RunStatus | str
  thread_id: str | None = None
  workflow_id: str | None = None
  workflow_version: str | None = None
  agent_id: str | None = None
  task_id: str | None = None
  current_node_id: str | None = None
  variables: dict[str, Any] = field(default_factory=dict)
  active_task_ids: list[str] = field(default_factory=list)
  message_refs: list[EntityRef] = field(default_factory=list)
  tool_call_refs: list[EntityRef] = field(default_factory=list)
  model_call_refs: list[EntityRef] = field(default_factory=list)
  artifact_refs: list[ArtifactRef] = field(default_factory=list)
  memory_refs: list[MemoryRef] = field(default_factory=list)
  checkpoint_id: str | None = None
  started_at: datetime | None = None
  updated_at: datetime = field(default_factory=utc_now)

  def __post_init__(self) -> None:
    if isinstance(self.status, str):
      self.status = RunStatus(self.status)

