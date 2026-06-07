"""Dead letter persistence."""

from __future__ import annotations

import json
import sqlite3

from agent_kernel.domain.serialization import to_primitive
from agent_kernel.domain.stability import DeadLetterItem


class DeadLetterStore:
  def __init__(self, conn: sqlite3.Connection) -> None:
    self._conn = conn

  def add(self, item: DeadLetterItem) -> None:
    self._conn.execute(
      """
      INSERT INTO dead_letters (
        item_id,
        target_type,
        target_id,
        reason,
        failure_type,
        retryable,
        item_json,
        created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
      """,
      (
        item.item_id,
        item.target_type,
        item.target_id,
        item.reason,
        item.failure_type,
        1 if item.retryable else 0,
        json.dumps(to_primitive(item), ensure_ascii=False, sort_keys=True),
        item.created_at.isoformat(),
      ),
    )

  def list_all(self) -> list[DeadLetterItem]:
    rows = self._conn.execute(
      "SELECT item_json FROM dead_letters ORDER BY created_at ASC, item_id ASC"
    ).fetchall()
    return [DeadLetterItem.from_dict(json.loads(row["item_json"])) for row in rows]

