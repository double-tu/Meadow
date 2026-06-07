"""Tool call persistence."""

from __future__ import annotations

import json
import sqlite3

from agent_kernel.domain.serialization import to_primitive
from agent_kernel.domain.tool_call import ToolCallRecord


class ToolCallStore:
  def __init__(self, conn: sqlite3.Connection) -> None:
    self._conn = conn

  def save(self, record: ToolCallRecord) -> None:
    self._conn.execute(
      """
      INSERT INTO tool_calls (
        tool_call_id,
        run_id,
        capability_id,
        status,
        idempotency_key,
        process_id,
        record_json,
        created_at,
        updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(tool_call_id) DO UPDATE SET
        status = excluded.status,
        process_id = excluded.process_id,
        record_json = excluded.record_json,
        updated_at = excluded.updated_at
      """,
      (
        record.tool_call_id,
        record.run_id,
        record.capability_id,
        record.status.value,
        record.idempotency_key,
        record.process_id,
        json.dumps(to_primitive(record), ensure_ascii=False, sort_keys=True),
        record.created_at.isoformat(),
        record.updated_at.isoformat(),
      ),
    )

  def get(self, tool_call_id: str) -> ToolCallRecord | None:
    row = self._conn.execute(
      "SELECT record_json FROM tool_calls WHERE tool_call_id = ?",
      (tool_call_id,),
    ).fetchone()
    if row is None:
      return None
    return ToolCallRecord.from_dict(json.loads(row["record_json"]))

  def list_by_run(self, run_id: str) -> list[ToolCallRecord]:
    rows = self._conn.execute(
      """
      SELECT record_json
      FROM tool_calls
      WHERE run_id = ?
      ORDER BY created_at ASC, tool_call_id ASC
      """,
      (run_id,),
    ).fetchall()
    return [ToolCallRecord.from_dict(json.loads(row["record_json"])) for row in rows]

  def list_all(self) -> list[ToolCallRecord]:
    rows = self._conn.execute(
      """
      SELECT record_json
      FROM tool_calls
      ORDER BY created_at ASC, tool_call_id ASC
      """
    ).fetchall()
    return [ToolCallRecord.from_dict(json.loads(row["record_json"])) for row in rows]
