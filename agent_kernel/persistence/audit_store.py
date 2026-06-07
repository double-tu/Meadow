"""Audit log persistence."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agent_kernel.domain.base import DomainModel, new_id, utc_now


@dataclass(slots=True)
class AuditRecord(DomainModel):
  audit_id: str
  action: str
  target_ref: str
  run_id: str | None = None
  actor_id: str | None = None
  decision: str | None = None
  payload: dict[str, Any] = field(default_factory=dict)
  created_at: datetime = field(default_factory=utc_now)

  @classmethod
  def create(
    cls,
    action: str,
    target_ref: str,
    run_id: str | None = None,
    actor_id: str | None = None,
    decision: str | None = None,
    payload: dict[str, Any] | None = None,
  ) -> "AuditRecord":
    return cls(
      audit_id=new_id("audit"),
      action=action,
      target_ref=target_ref,
      run_id=run_id,
      actor_id=actor_id,
      decision=decision,
      payload=payload or {},
    )


class AuditStore:
  def __init__(self, conn: sqlite3.Connection) -> None:
    self._conn = conn

  def add(self, record: AuditRecord) -> None:
    self._conn.execute(
      """
      INSERT INTO audit_records (
        audit_id,
        run_id,
        actor_id,
        action,
        target_ref,
        decision,
        payload_json,
        created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
      """,
      (
        record.audit_id,
        record.run_id,
        record.actor_id,
        record.action,
        record.target_ref,
        record.decision,
        json.dumps(record.payload, ensure_ascii=False, sort_keys=True),
        record.created_at.isoformat(),
      ),
    )

  def list_by_run(self, run_id: str) -> list[AuditRecord]:
    rows = self._conn.execute(
      """
      SELECT audit_id, run_id, actor_id, action, target_ref, decision, payload_json, created_at
      FROM audit_records
      WHERE run_id = ?
      ORDER BY created_at ASC, audit_id ASC
      """,
      (run_id,),
    ).fetchall()
    return [
      AuditRecord(
        audit_id=row["audit_id"],
        run_id=row["run_id"],
        actor_id=row["actor_id"],
        action=row["action"],
        target_ref=row["target_ref"],
        decision=row["decision"],
        payload=json.loads(row["payload_json"]),
        created_at=datetime.fromisoformat(row["created_at"]),
      )
      for row in rows
    ]
