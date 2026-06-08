"""Collaboration workbench domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from agent_kernel.domain.base import DomainModel, utc_now
from agent_kernel.domain.identifiers import ArtifactRef
from agent_kernel.domain.interaction import ParticipantKind


class CollaborationWorkbenchKind(StrEnum):
  GROUP_CHAT = "group_chat"
  CLI_COLLABORATION = "cli_collaboration"
  TECHNICAL_REVIEW = "technical_review"
  PARALLEL_DELEGATION = "parallel_delegation"


class CollaborationWorkbenchStatus(StrEnum):
  CREATED = "created"
  RUNNING = "running"
  PAUSED = "paused"
  COMPLETED = "completed"
  CANCELLED = "cancelled"


class WorkbenchTaskSliceStatus(StrEnum):
  PLANNED = "planned"
  RUNNING = "running"
  REVIEW = "review"
  DONE = "done"
  FAILED = "failed"
  CANCELLED = "cancelled"


@dataclass(slots=True)
class WorkbenchMember(DomainModel):
  member_id: str
  participant_id: str
  role: str
  kind: ParticipantKind | str = ParticipantKind.AGENT
  agent_type: str | None = None
  connector_id: str | None = None
  agent_session_id: str | None = None
  labels: list[str] = field(default_factory=list)
  metadata: dict[str, Any] = field(default_factory=dict)
  created_at: datetime = field(default_factory=utc_now)
  updated_at: datetime = field(default_factory=utc_now)

  def __post_init__(self) -> None:
    if isinstance(self.kind, str):
      self.kind = ParticipantKind(self.kind)


@dataclass(slots=True)
class WorkbenchTaskSlice(DomainModel):
  slice_id: str
  title: str
  objective: str
  role: str | None = None
  assignee_member_id: str | None = None
  taskboard_item_id: str | None = None
  delegation_task_id: str | None = None
  status: WorkbenchTaskSliceStatus | str = WorkbenchTaskSliceStatus.PLANNED
  result_artifact_refs: list[ArtifactRef] = field(default_factory=list)
  metadata: dict[str, Any] = field(default_factory=dict)
  created_at: datetime = field(default_factory=utc_now)
  updated_at: datetime = field(default_factory=utc_now)

  def __post_init__(self) -> None:
    if isinstance(self.status, str):
      self.status = WorkbenchTaskSliceStatus(self.status)


@dataclass(slots=True)
class CollaborationWorkbench(DomainModel):
  workbench_id: str
  title: str
  kind: CollaborationWorkbenchKind | str
  objective: str
  status: CollaborationWorkbenchStatus | str = CollaborationWorkbenchStatus.CREATED
  parent_run_id: str | None = None
  channel_id: str | None = None
  group_chat_id: str | None = None
  member_ids: list[str] = field(default_factory=list)
  task_slice_ids: list[str] = field(default_factory=list)
  delegation_task_ids: list[str] = field(default_factory=list)
  taskboard_item_ids: list[str] = field(default_factory=list)
  metadata: dict[str, Any] = field(default_factory=dict)
  created_at: datetime = field(default_factory=utc_now)
  updated_at: datetime = field(default_factory=utc_now)

  def __post_init__(self) -> None:
    if isinstance(self.kind, str):
      self.kind = CollaborationWorkbenchKind(self.kind)
    if isinstance(self.status, str):
      self.status = CollaborationWorkbenchStatus(self.status)
