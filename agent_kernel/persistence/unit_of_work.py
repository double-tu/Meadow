"""Transaction boundary for persistence operations."""

from __future__ import annotations

import sqlite3
from types import TracebackType

from agent_kernel.persistence.artifact_store import ArtifactStore
from agent_kernel.persistence.agent_store import AgentSessionStore, AgentTaskResultStore, MailboxStore
from agent_kernel.persistence.approval_store import ApprovalStore
from agent_kernel.persistence.autonomy_store import AutonomyStore
from agent_kernel.persistence.audit_store import AuditStore
from agent_kernel.persistence.checkpoint_store import CheckpointStore
from agent_kernel.persistence.dead_letter_store import DeadLetterStore
from agent_kernel.persistence.event_store import EventStore
from agent_kernel.persistence.grant_store import GrantStore
from agent_kernel.persistence.interaction_store import InteractionStore
from agent_kernel.persistence.memory_store import MemoryStore
from agent_kernel.persistence.outbox_store import OutboxStore
from agent_kernel.persistence.sqlite import initialize_schema
from agent_kernel.persistence.state_store import StateStore
from agent_kernel.persistence.step_store import StepStore
from agent_kernel.persistence.tool_call_store import ToolCallStore


class UnitOfWork:
  def __init__(self, conn: sqlite3.Connection) -> None:
    self.conn = conn
    initialize_schema(self.conn)
    self.events = EventStore(self.conn)
    self.states = StateStore(self.conn)
    self.checkpoints = CheckpointStore(self.conn)
    self.artifacts = ArtifactStore(self.conn)
    self.approvals = ApprovalStore(self.conn)
    self.autonomy = AutonomyStore(self.conn)
    self.audit = AuditStore(self.conn)
    self.grants = GrantStore(self.conn)
    self.interactions = InteractionStore(self.conn)
    self.agent_sessions = AgentSessionStore(self.conn)
    self.agent_results = AgentTaskResultStore(self.conn)
    self.mailbox = MailboxStore(self.conn)
    self.memory = MemoryStore(self.conn)
    self.steps = StepStore(self.conn)
    self.tool_calls = ToolCallStore(self.conn)
    self.dead_letters = DeadLetterStore(self.conn)
    self.outbox = OutboxStore(self.conn)

  def __enter__(self) -> UnitOfWork:
    self.conn.execute("BEGIN")
    return self

  def __exit__(
    self,
    exc_type: type[BaseException] | None,
    exc: BaseException | None,
    traceback: TracebackType | None,
  ) -> None:
    if exc_type is None:
      self.conn.commit()
    else:
      self.conn.rollback()
