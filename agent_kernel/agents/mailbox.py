"""Mailbox application helpers."""

from agent_kernel.domain.agent import MailboxMessage
from agent_kernel.domain.base import new_id


def build_mailbox_message(
  recipient_session_id: str,
  content: dict[str, object],
  sender_session_id: str | None = None,
  mailbox_id: str | None = None,
) -> MailboxMessage:
  return MailboxMessage(
    message_id=new_id("msg"),
    mailbox_id=mailbox_id or f"mailbox_{recipient_session_id}",
    sender_session_id=sender_session_id,
    recipient_session_id=recipient_session_id,
    content=content,
  )

