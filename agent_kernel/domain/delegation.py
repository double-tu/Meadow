"""Agent delegation task lifecycle models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from agent_kernel.domain.base import DomainModel, new_id, utc_now
from agent_kernel.domain.errors import DomainValidationError
from agent_kernel.domain.identifiers import ArtifactRef


class DelegationStatus(StrEnum):
  RUNNING = "running"
  COMPLETED = "completed"
  FAILED = "failed"
  CANCELLED = "cancelled"
  UNKNOWN = "unknown"


TERMINAL_DELEGATION_STATUSES = {
  DelegationStatus.COMPLETED,
  DelegationStatus.FAILED,
  DelegationStatus.CANCELLED,
  DelegationStatus.UNKNOWN,
}


@dataclass(slots=True)
class DelegationTask(DomainModel):
  task: str
  parent_run_id: str
  connector_id: str
  connector_session_id: str
  task_id: str = field(default_factory=lambda: new_id("delegation"))
  status: DelegationStatus | str = DelegationStatus.RUNNING
  agent_type: str | None = None
  parent_agent_id: str | None = None
  parent_session_id: str | None = None
  message_id: str | None = None
  turn_id: str | None = None
  output: dict[str, Any] | None = None
  result_artifact_refs: list[ArtifactRef] = field(default_factory=list)
  error: dict[str, Any] | None = None
  metadata: dict[str, Any] = field(default_factory=dict)
  created_at: datetime = field(default_factory=utc_now)
  updated_at: datetime = field(default_factory=utc_now)
  completed_at: datetime | None = None

  def __post_init__(self) -> None:
    if isinstance(self.status, str):
      self.status = DelegationStatus(self.status)
    if not self.task.strip():
      raise DomainValidationError("DelegationTask.task is required.")
    if not self.parent_run_id:
      raise DomainValidationError("DelegationTask.parent_run_id is required.")
    if not self.connector_id:
      raise DomainValidationError("DelegationTask.connector_id is required.")
    if not self.connector_session_id:
      raise DomainValidationError("DelegationTask.connector_session_id is required.")

  @property
  def is_terminal(self) -> bool:
    return self.status in TERMINAL_DELEGATION_STATUSES

  def mark_running_message(self, message_id: str) -> "DelegationTask":
    self.message_id = message_id
    self.updated_at = utc_now()
    return self

  def mark_completed(
    self,
    *,
    turn_id: str,
    output: dict[str, Any],
    result_artifact_refs: list[ArtifactRef] | None = None,
  ) -> "DelegationTask":
    self.status = DelegationStatus.COMPLETED
    self.turn_id = turn_id
    self.output = output
    self.result_artifact_refs = result_artifact_refs or []
    self.error = None
    self.updated_at = utc_now()
    self.completed_at = self.updated_at
    return self

  def mark_failed(self, error: dict[str, Any]) -> "DelegationTask":
    self.status = DelegationStatus.FAILED
    self.error = error
    self.updated_at = utc_now()
    self.completed_at = self.updated_at
    return self

  def mark_cancelled(self, reason: str) -> "DelegationTask":
    self.status = DelegationStatus.CANCELLED
    self.error = {"type": "cancelled", "message": reason}
    self.updated_at = utc_now()
    self.completed_at = self.updated_at
    return self


@dataclass(slots=True)
class DelegationTaskReport(DomainModel):
  task_id: str
  status: DelegationStatus | str
  parent_run_id: str | None = None
  connector_id: str | None = None
  connector_session_id: str | None = None
  agent_type: str | None = None
  task: str | None = None
  output: dict[str, Any] | None = None
  result_artifact_refs: list[ArtifactRef] = field(default_factory=list)
  error: dict[str, Any] | None = None
  message: str | None = None
  created_at: datetime | None = None
  updated_at: datetime | None = None
  completed_at: datetime | None = None

  def __post_init__(self) -> None:
    if isinstance(self.status, str):
      self.status = DelegationStatus(self.status)

  @classmethod
  def from_task(cls, task: DelegationTask) -> "DelegationTaskReport":
    return cls(
      task_id=task.task_id,
      status=task.status,
      parent_run_id=task.parent_run_id,
      connector_id=task.connector_id,
      connector_session_id=task.connector_session_id,
      agent_type=task.agent_type,
      task=task.task,
      output=task.output,
      result_artifact_refs=task.result_artifact_refs,
      error=task.error,
      created_at=task.created_at,
      updated_at=task.updated_at,
      completed_at=task.completed_at,
    )

  @classmethod
  def unknown(cls, task_id: str, *, message: str = "delegation task not found") -> "DelegationTaskReport":
    return cls(task_id=task_id, status=DelegationStatus.UNKNOWN, message=message)
