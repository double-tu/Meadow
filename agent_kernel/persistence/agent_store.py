"""Agent session and mailbox persistence."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

from agent_kernel.domain.agent import (
  AgentSession,
  AgentTaskResult,
  MailboxMessage,
  MailboxMessageStatus,
)
from agent_kernel.domain.serialization import to_primitive


class AgentSessionStore:
  def __init__(self, conn: sqlite3.Connection) -> None:
    self._conn = conn

  def save(self, session: AgentSession) -> None:
    self._conn.execute(
      """
      INSERT INTO agent_sessions (
        session_id,
        agent_id,
        status,
        parent_session_id,
        task_id,
        mailbox_id,
        state_json,
        created_at,
        updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(session_id) DO UPDATE SET
        status = excluded.status,
        parent_session_id = excluded.parent_session_id,
        task_id = excluded.task_id,
        mailbox_id = excluded.mailbox_id,
        state_json = excluded.state_json,
        updated_at = excluded.updated_at
      """,
      (
        session.session_id,
        session.agent_id,
        session.status.value,
        session.parent_session_id,
        session.task_id,
        session.mailbox_id,
        json.dumps(to_primitive(session), ensure_ascii=False, sort_keys=True),
        session.created_at.isoformat(),
        session.updated_at.isoformat(),
      ),
    )

  def get(self, session_id: str) -> AgentSession | None:
    row = self._conn.execute(
      "SELECT state_json FROM agent_sessions WHERE session_id = ?",
      (session_id,),
    ).fetchone()
    if row is None:
      return None
    return AgentSession.from_dict(json.loads(row["state_json"]))

  def list_children(self, parent_session_id: str) -> list[AgentSession]:
    rows = self._conn.execute(
      """
      SELECT state_json
      FROM agent_sessions
      WHERE parent_session_id = ?
      ORDER BY created_at ASC, session_id ASC
      """,
      (parent_session_id,),
    ).fetchall()
    return [AgentSession.from_dict(json.loads(row["state_json"])) for row in rows]


class MailboxStore:
  def __init__(self, conn: sqlite3.Connection) -> None:
    self._conn = conn

  def send(self, message: MailboxMessage) -> None:
    self._conn.execute(
      """
      INSERT INTO mailbox_messages (
        message_id,
        mailbox_id,
        sender_session_id,
        recipient_session_id,
        status,
        message_json,
        created_at,
        updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(message_id) DO UPDATE SET
        status = excluded.status,
        message_json = excluded.message_json,
        updated_at = excluded.updated_at
      """,
      (
        message.message_id,
        message.mailbox_id,
        message.sender_session_id,
        message.recipient_session_id,
        message.status.value,
        json.dumps(to_primitive(message), ensure_ascii=False, sort_keys=True),
        message.created_at.isoformat(),
        message.updated_at.isoformat(),
      ),
    )

  def list_pending(self, recipient_session_id: str) -> list[MailboxMessage]:
    rows = self._conn.execute(
      """
      SELECT message_json
      FROM mailbox_messages
      WHERE recipient_session_id = ?
        AND status = ?
      ORDER BY created_at ASC, message_id ASC
      """,
      (recipient_session_id, MailboxMessageStatus.PENDING.value),
    ).fetchall()
    return [MailboxMessage.from_dict(json.loads(row["message_json"])) for row in rows]

  def mark_handled(self, message_id: str) -> None:
    row = self._conn.execute(
      "SELECT message_json FROM mailbox_messages WHERE message_id = ?",
      (message_id,),
    ).fetchone()
    if row is None:
      raise KeyError(f"Mailbox message not found: {message_id}")
    message = MailboxMessage.from_dict(json.loads(row["message_json"]))
    message = replace(message, status=MailboxMessageStatus.HANDLED)
    self.send(message)


class AgentTaskResultStore:
  def __init__(self, conn: sqlite3.Connection) -> None:
    self._conn = conn

  def save(self, result: AgentTaskResult) -> None:
    self._conn.execute(
      """
      INSERT INTO agent_task_results (
        result_id,
        session_id,
        task_id,
        ok,
        result_json,
        created_at
      ) VALUES (?, ?, ?, ?, ?, ?)
      """,
      (
        result.result_id,
        result.session_id,
        result.task_id,
        1 if result.ok else 0,
        json.dumps(to_primitive(result), ensure_ascii=False, sort_keys=True),
        result.created_at.isoformat(),
      ),
    )

  def latest_for_session(self, session_id: str) -> AgentTaskResult | None:
    row = self._conn.execute(
      """
      SELECT result_json
      FROM agent_task_results
      WHERE session_id = ?
      ORDER BY created_at DESC, result_id DESC
      LIMIT 1
      """,
      (session_id,),
    ).fetchone()
    if row is None:
      return None
    return AgentTaskResult.from_dict(json.loads(row["result_json"]))
