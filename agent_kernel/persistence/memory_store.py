"""Memory item persistence."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

from agent_kernel.domain.base import utc_now
from agent_kernel.domain.memory import MemoryItem
from agent_kernel.domain.serialization import to_primitive


class MemoryStore:
  def __init__(self, conn: sqlite3.Connection) -> None:
    self._conn = conn

  def save(self, item: MemoryItem) -> None:
    self._conn.execute(
      """
      INSERT INTO memory_items (
        memory_id,
        memory_type,
        scope,
        sensitivity,
        importance,
        memory_json,
        created_at,
        last_used_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(memory_id) DO UPDATE SET
        sensitivity = excluded.sensitivity,
        importance = excluded.importance,
        memory_json = excluded.memory_json,
        last_used_at = excluded.last_used_at
      """,
      (
        item.memory_id,
        item.memory_type,
        item.scope,
        item.sensitivity,
        item.importance,
        json.dumps(to_primitive(item), ensure_ascii=False, sort_keys=True),
        item.created_at.isoformat(),
        item.last_used_at.isoformat() if item.last_used_at else None,
      ),
    )

  def get(self, memory_id: str) -> MemoryItem | None:
    row = self._conn.execute(
      "SELECT memory_json FROM memory_items WHERE memory_id = ?",
      (memory_id,),
    ).fetchone()
    if row is None:
      return None
    return MemoryItem.from_dict(json.loads(row["memory_json"]))

  def list_by_scope(
    self,
    scope: str,
    memory_type: str | None = None,
    limit: int = 20,
  ) -> list[MemoryItem]:
    if memory_type is None:
      rows = self._conn.execute(
        """
        SELECT memory_json
        FROM memory_items
        WHERE scope = ?
        ORDER BY COALESCE(importance, 0) DESC, created_at DESC
        LIMIT ?
        """,
        (scope, limit),
      ).fetchall()
    else:
      rows = self._conn.execute(
        """
        SELECT memory_json
        FROM memory_items
        WHERE scope = ?
          AND memory_type = ?
        ORDER BY COALESCE(importance, 0) DESC, created_at DESC
        LIMIT ?
        """,
        (scope, memory_type, limit),
      ).fetchall()
    return [MemoryItem.from_dict(json.loads(row["memory_json"])) for row in rows]

  def mark_used(self, memory_id: str) -> None:
    item = self.get(memory_id)
    if item is None:
      raise KeyError(f"Memory item not found: {memory_id}")
    self.save(replace(item, last_used_at=utc_now()))

