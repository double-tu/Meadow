"""Checkpoint persistence."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from agent_kernel.domain.base import DomainModel, new_id, utc_now
from agent_kernel.domain.run import RunState
from agent_kernel.domain.serialization import to_primitive


@dataclass(slots=True)
class CheckpointRecord(DomainModel):
  checkpoint_id: str
  run_id: str
  state: RunState
  event_id: str | None
  created_at: datetime


class CheckpointStore:
  def __init__(self, conn: sqlite3.Connection) -> None:
    self._conn = conn

  def save(
    self,
    run_id: str,
    state: RunState,
    event_id: str | None = None,
    checkpoint_id: str | None = None,
  ) -> CheckpointRecord:
    record = CheckpointRecord(
      checkpoint_id=checkpoint_id or new_id("chk"),
      run_id=run_id,
      state=state,
      event_id=event_id,
      created_at=utc_now(),
    )
    self._conn.execute(
      """
      INSERT INTO checkpoints (checkpoint_id, run_id, state_json, event_id, created_at)
      VALUES (?, ?, ?, ?, ?)
      """,
      (
        record.checkpoint_id,
        record.run_id,
        json.dumps(to_primitive(record.state), ensure_ascii=False, sort_keys=True),
        record.event_id,
        record.created_at.isoformat(),
      ),
    )
    return record

  def latest_for_run(self, run_id: str) -> CheckpointRecord | None:
    row = self._conn.execute(
      """
      SELECT checkpoint_id, run_id, state_json, event_id, created_at
      FROM checkpoints
      WHERE run_id = ?
      ORDER BY created_at DESC, checkpoint_id DESC
      LIMIT 1
      """,
      (run_id,),
    ).fetchone()
    if row is None:
      return None
    return CheckpointRecord(
      checkpoint_id=row["checkpoint_id"],
      run_id=row["run_id"],
      state=RunState.from_dict(json.loads(row["state_json"])),
      event_id=row["event_id"],
      created_at=datetime.fromisoformat(row["created_at"]),
    )

  def list_by_run(self, run_id: str) -> list[CheckpointRecord]:
    rows = self._conn.execute(
      """
      SELECT checkpoint_id, run_id, state_json, event_id, created_at
      FROM checkpoints
      WHERE run_id = ?
      ORDER BY created_at ASC, checkpoint_id ASC
      """,
      (run_id,),
    ).fetchall()
    return [
      CheckpointRecord(
        checkpoint_id=row["checkpoint_id"],
        run_id=row["run_id"],
        state=RunState.from_dict(json.loads(row["state_json"])),
        event_id=row["event_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
      )
      for row in rows
    ]
