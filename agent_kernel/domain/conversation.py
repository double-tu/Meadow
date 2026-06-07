"""Conversation and objective domain models."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from agent_kernel.domain.base import DomainModel, utc_now
from agent_kernel.domain.identifiers import ArtifactRef, EntityRef


class MessageRole(StrEnum):
  USER = "user"
  ASSISTANT = "assistant"
  TOOL = "tool"
  SYSTEM = "system"
  HUMAN_APPROVAL = "human_approval"


@dataclass(slots=True)
class Message(DomainModel):
  message_id: str
  role: MessageRole | str
  content: dict[str, Any]
  artifact_refs: list[ArtifactRef] = field(default_factory=list)
  created_at: datetime = field(default_factory=utc_now)

  def __post_init__(self) -> None:
    if isinstance(self.role, str):
      self.role = MessageRole(self.role)


@dataclass(slots=True)
class Thread(DomainModel):
  thread_id: str
  workspace_id: str
  title: str | None = None
  message_refs: list[EntityRef] = field(default_factory=list)
  created_at: datetime = field(default_factory=utc_now)
  updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class Objective(DomainModel):
  objective_id: str
  description: str
  thread_id: str | None = None
  acceptance_criteria: list[str] = field(default_factory=list)
  created_at: datetime = field(default_factory=utc_now)

