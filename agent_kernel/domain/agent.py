"""Agent domain models."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from agent_kernel.domain.base import DomainModel, utc_now
from agent_kernel.domain.states import AgentStatus


class AgentMode(StrEnum):
  ONESHOT = "oneshot"
  PERSISTENT = "persistent"
  TEAM_MEMBER = "team_member"
  REMOTE = "remote"
  HUMAN_PROXY = "human_proxy"


class MailboxMessageStatus(StrEnum):
  PENDING = "pending"
  DELIVERED = "delivered"
  HANDLED = "handled"
  CANCELLED = "cancelled"


@dataclass(slots=True)
class AgentSpec(DomainModel):
  agent_id: str
  name: str
  mode: AgentMode | str
  model_ref: str
  role: str | None = None
  instructions: str | None = None
  toolset: list[str] = field(default_factory=list)
  memory_profile: str | None = None
  isolation: Literal["shared", "worktree", "sandbox", "remote"] = "sandbox"
  permission_policy_id: str | None = None

  def __post_init__(self) -> None:
    if isinstance(self.mode, str):
      self.mode = AgentMode(self.mode)


@dataclass(slots=True)
class AgentSession(DomainModel):
  session_id: str
  agent_id: str
  status: AgentStatus | str
  parent_session_id: str | None = None
  task_id: str | None = None
  mailbox_id: str | None = None
  created_at: datetime = field(default_factory=utc_now)
  updated_at: datetime = field(default_factory=utc_now)

  def __post_init__(self) -> None:
    if isinstance(self.status, str):
      self.status = AgentStatus(self.status)


@dataclass(slots=True)
class MailboxMessage(DomainModel):
  message_id: str
  mailbox_id: str
  sender_session_id: str | None
  recipient_session_id: str
  content: dict[str, Any]
  status: MailboxMessageStatus | str = MailboxMessageStatus.PENDING
  causal_event_id: str | None = None
  created_at: datetime = field(default_factory=utc_now)
  updated_at: datetime = field(default_factory=utc_now)

  def __post_init__(self) -> None:
    if isinstance(self.status, str):
      self.status = MailboxMessageStatus(self.status)


@dataclass(slots=True)
class AgentTaskResult(DomainModel):
  result_id: str
  session_id: str
  task_id: str | None
  ok: bool
  output: dict[str, Any] = field(default_factory=dict)
  error: str | None = None
  created_at: datetime = field(default_factory=utc_now)
