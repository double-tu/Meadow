"""Structured agent intervention and mailbox protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from agent_kernel.agents.mailbox import build_mailbox_message
from agent_kernel.domain.agent import MailboxMessage
from agent_kernel.domain.base import new_id, utc_now


class AgentMessageKind(StrEnum):
  MESSAGE = "message"
  KEY_INFO = "keyinfo"
  INTERVENTION = "intervention"
  STOP = "stop"
  PAUSE = "pause"
  RESUME = "resume"
  STATUS = "status"
  PERMISSION_REQUEST = "permission_request"
  PERMISSION_RESPONSE = "permission_response"


@dataclass(slots=True)
class AgentProtocolMessage:
  kind: AgentMessageKind | str
  content: dict[str, Any]
  sender_session_id: str | None
  recipient_session_id: str
  message_id: str = field(default_factory=lambda: new_id("agent_proto_msg"))
  run_id: str | None = None
  task_id: str | None = None
  priority: str = "normal"
  created_at: str = field(default_factory=lambda: utc_now().isoformat())

  def __post_init__(self) -> None:
    if isinstance(self.kind, str):
      self.kind = AgentMessageKind(self.kind)

  def to_mailbox(self) -> MailboxMessage:
    return build_mailbox_message(
      recipient_session_id=self.recipient_session_id,
      sender_session_id=self.sender_session_id,
      content={
        "protocol": "meadow.agent_mailbox.v1",
        "kind": self.kind.value,
        "message_id": self.message_id,
        "run_id": self.run_id,
        "task_id": self.task_id,
        "priority": self.priority,
        "created_at": self.created_at,
        "content": self.content,
      },
    )


class AgentInterventionChannel:
  """Builds mailbox messages for intervention without owning persistence."""

  def keyinfo(
    self,
    *,
    recipient_session_id: str,
    content: dict[str, Any],
    sender_session_id: str | None = None,
    run_id: str | None = None,
  ) -> MailboxMessage:
    return AgentProtocolMessage(
      kind=AgentMessageKind.KEY_INFO,
      sender_session_id=sender_session_id,
      recipient_session_id=recipient_session_id,
      run_id=run_id,
      priority="high",
      content=content,
    ).to_mailbox()

  def intervene(
    self,
    *,
    recipient_session_id: str,
    instruction: str,
    sender_session_id: str | None = None,
    run_id: str | None = None,
  ) -> MailboxMessage:
    return AgentProtocolMessage(
      kind=AgentMessageKind.INTERVENTION,
      sender_session_id=sender_session_id,
      recipient_session_id=recipient_session_id,
      run_id=run_id,
      priority="high",
      content={"instruction": instruction},
    ).to_mailbox()

  def stop(
    self,
    *,
    recipient_session_id: str,
    reason: str,
    sender_session_id: str | None = None,
    run_id: str | None = None,
  ) -> MailboxMessage:
    return AgentProtocolMessage(
      kind=AgentMessageKind.STOP,
      sender_session_id=sender_session_id,
      recipient_session_id=recipient_session_id,
      run_id=run_id,
      priority="high",
      content={"reason": reason},
    ).to_mailbox()
