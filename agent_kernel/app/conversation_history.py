"""Conversation history compaction helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from agent_kernel.domain.memory import MemoryItem
from agent_kernel.memory import MemoryFacade


class ConversationMessageLike(Protocol):
  message_id: str
  role: str
  content: str


@dataclass(slots=True)
class ConversationCompactionResult:
  memory: MemoryItem | None
  compacted_message_ids: list[str]


class ConversationHistoryCompactor:
  """Stores older chat turns as episodic memory without deleting messages."""

  def __init__(
    self,
    memory: MemoryFacade,
    *,
    threshold_messages: int = 18,
    keep_recent_messages: int = 10,
  ) -> None:
    if keep_recent_messages < 2:
      raise ValueError("keep_recent_messages must be at least 2.")
    self._memory = memory
    self._threshold_messages = threshold_messages
    self._keep_recent_messages = keep_recent_messages

  def compact(
    self,
    *,
    scope: str,
    messages: list[ConversationMessageLike],
    already_compacted_message_ids: list[str] | None = None,
    task_id: str | None = None,
  ) -> ConversationCompactionResult:
    compacted_ids = set(already_compacted_message_ids or [])
    eligible = [
      message
      for message in messages
      if message.role in {"user", "assistant"}
      and message.content.strip()
      and message.message_id not in compacted_ids
    ]
    if len(eligible) <= self._threshold_messages:
      return ConversationCompactionResult(memory=None, compacted_message_ids=[])
    older = eligible[: -self._keep_recent_messages]
    if not older:
      return ConversationCompactionResult(memory=None, compacted_message_ids=[])
    observations = [_message_observation(message) for message in older]
    item = self._memory.write_episode(
      scope=scope,
      task_id=task_id,
      event_ids=[],
      observations=observations,
      outcome=_conversation_outcome(older),
      importance=0.65,
      created_by="conversation_history_compactor",
    )
    item.content = {
      **item.content,
      "kind": "conversation_history_summary",
      "message_ids": [message.message_id for message in older],
    }
    self._memory.save(item)
    return ConversationCompactionResult(
      memory=item,
      compacted_message_ids=[message.message_id for message in older],
    )


def _message_observation(message: ConversationMessageLike) -> str:
  content = message.content.strip()
  if len(content) > 500:
    content = content[:500] + "...[truncated]"
  return f"{message.role}: {content}"


def _conversation_outcome(messages: list[ConversationMessageLike]) -> str:
  first = messages[0].content.strip() if messages else ""
  last = messages[-1].content.strip() if messages else ""
  return f"Compacted {len(messages)} older chat messages. First: {first[:120]} Last: {last[:120]}"
