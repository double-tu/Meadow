"""Transactional outbox persistence."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from agent_kernel.domain.base import DomainModel, new_id, utc_now


@dataclass(slots=True)
class OutboxItem(DomainModel):
  item_id: str
  topic: str
  payload: dict[str, Any]
  status: Literal["pending", "published", "failed"] = "pending"
  created_at: datetime = field(default_factory=utc_now)
  updated_at: datetime = field(default_factory=utc_now)

  @classmethod
  def create(cls, topic: str, payload: dict[str, Any]) -> "OutboxItem":
    return cls(item_id=new_id("outbox"), topic=topic, payload=payload)


class OutboxStore:
  def __init__(self, conn: sqlite3.Connection) -> None:
    self._conn = conn

  def add(self, item: OutboxItem) -> None:
    self._conn.execute(
      """
      INSERT INTO outbox_items (item_id, topic, payload_json, status, created_at, updated_at)
      VALUES (?, ?, ?, ?, ?, ?)
      """,
      (
        item.item_id,
        item.topic,
        json.dumps(item.payload, ensure_ascii=False, sort_keys=True),
        item.status,
        item.created_at.isoformat(),
        item.updated_at.isoformat(),
      ),
    )

  def list_pending(self) -> list[OutboxItem]:
    rows = self._conn.execute(
      """
      SELECT item_id, topic, payload_json, status, created_at, updated_at
      FROM outbox_items
      WHERE status = 'pending'
      ORDER BY created_at ASC, item_id ASC
      """
    ).fetchall()
    return [
      OutboxItem(
        item_id=row["item_id"],
        topic=row["topic"],
        payload=json.loads(row["payload_json"]),
        status=row["status"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
      )
      for row in rows
    ]

