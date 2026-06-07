"""Recovery job persistence."""

from __future__ import annotations

import json
import sqlite3

from agent_kernel.domain.serialization import to_primitive
from agent_kernel.domain.stability import RecoveryJob


class RecoveryStore:
  def __init__(self, conn: sqlite3.Connection) -> None:
    self._conn = conn

  def save(self, job: RecoveryJob) -> None:
    self._conn.execute(
      """
      INSERT INTO recovery_jobs (
        recovery_id,
        target_type,
        target_id,
        status,
        job_json,
        created_at,
        updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(recovery_id) DO UPDATE SET
        status = excluded.status,
        job_json = excluded.job_json,
        updated_at = excluded.updated_at
      """,
      (
        job.recovery_id,
        job.target_type,
        job.target_id,
        job.status.value,
        json.dumps(to_primitive(job), ensure_ascii=False, sort_keys=True),
        job.created_at.isoformat(),
        job.updated_at.isoformat(),
      ),
    )

  def get(self, recovery_id: str) -> RecoveryJob | None:
    row = self._conn.execute(
      "SELECT job_json FROM recovery_jobs WHERE recovery_id = ?",
      (recovery_id,),
    ).fetchone()
    if row is None:
      return None
    return RecoveryJob.from_dict(json.loads(row["job_json"]))

  def list_by_target(self, target_type: str, target_id: str) -> list[RecoveryJob]:
    rows = self._conn.execute(
      """
      SELECT job_json
      FROM recovery_jobs
      WHERE target_type = ? AND target_id = ?
      ORDER BY created_at ASC, recovery_id ASC
      """,
      (target_type, target_id),
    ).fetchall()
    return [RecoveryJob.from_dict(json.loads(row["job_json"])) for row in rows]

  def list_all(self) -> list[RecoveryJob]:
    rows = self._conn.execute(
      "SELECT job_json FROM recovery_jobs ORDER BY created_at ASC, recovery_id ASC"
    ).fetchall()
    return [RecoveryJob.from_dict(json.loads(row["job_json"])) for row in rows]
