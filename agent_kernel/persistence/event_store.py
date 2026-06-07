"""Runtime event persistence."""

from __future__ import annotations

import json
import sqlite3

from agent_kernel.domain.events import RuntimeEvent
from agent_kernel.domain.serialization import to_primitive


class EventStore:
  def __init__(self, conn: sqlite3.Connection) -> None:
    self._conn = conn

  def append(self, event: RuntimeEvent) -> None:
    self._conn.execute(
      """
      INSERT INTO runtime_events (
        event_id,
        run_id,
        event_type,
        timestamp,
        node_id,
        step_id,
        agent_id,
        task_id,
        causal_id,
        payload_json,
        artifact_refs_json
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      """,
      (
        event.event_id,
        event.run_id,
        event.event_type.value,
        event.timestamp.isoformat(),
        event.node_id,
        event.step_id,
        event.agent_id,
        event.task_id,
        event.causal_id,
        json.dumps(to_primitive(event.payload), ensure_ascii=False, sort_keys=True),
        json.dumps(to_primitive(event.artifact_refs), ensure_ascii=False, sort_keys=True),
      ),
    )

  def list_by_run(self, run_id: str) -> list[RuntimeEvent]:
    rows = self._conn.execute(
      """
      SELECT
        event_id,
        event_type,
        run_id,
        timestamp,
        node_id,
        step_id,
        agent_id,
        task_id,
        causal_id,
        payload_json,
        artifact_refs_json
      FROM runtime_events
      WHERE run_id = ?
      ORDER BY timestamp ASC, event_id ASC
      """,
      (run_id,),
    ).fetchall()
    return [self._row_to_event(row) for row in rows]

  def get(self, event_id: str) -> RuntimeEvent | None:
    row = self._conn.execute(
      """
      SELECT
        event_id,
        event_type,
        run_id,
        timestamp,
        node_id,
        step_id,
        agent_id,
        task_id,
        causal_id,
        payload_json,
        artifact_refs_json
      FROM runtime_events
      WHERE event_id = ?
      """,
      (event_id,),
    ).fetchone()
    if row is None:
      return None
    return self._row_to_event(row)

  def list_all(self) -> list[RuntimeEvent]:
    rows = self._conn.execute(
      """
      SELECT
        event_id,
        event_type,
        run_id,
        timestamp,
        node_id,
        step_id,
        agent_id,
        task_id,
        causal_id,
        payload_json,
        artifact_refs_json
      FROM runtime_events
      ORDER BY timestamp ASC, event_id ASC
      """
    ).fetchall()
    return [self._row_to_event(row) for row in rows]

  @staticmethod
  def _row_to_event(row: sqlite3.Row) -> RuntimeEvent:
    return RuntimeEvent.from_dict(
      {
        "event_id": row["event_id"],
        "event_type": row["event_type"],
        "run_id": row["run_id"],
        "timestamp": row["timestamp"],
        "node_id": row["node_id"],
        "step_id": row["step_id"],
        "agent_id": row["agent_id"],
        "task_id": row["task_id"],
        "causal_id": row["causal_id"],
        "payload": json.loads(row["payload_json"]),
        "artifact_refs": json.loads(row["artifact_refs_json"]),
      }
    )
