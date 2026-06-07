"""Node step persistence."""

from __future__ import annotations

import json
import sqlite3

from agent_kernel.domain.step import NodeStepRecord


class StepStore:
  def __init__(self, conn: sqlite3.Connection) -> None:
    self._conn = conn

  def save(self, step: NodeStepRecord) -> None:
    self._conn.execute(
      """
      INSERT INTO node_steps (
        step_id,
        run_id,
        node_id,
        status,
        attempt,
        idempotency_key,
        lease_id,
        error,
        state_json,
        created_at,
        updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(step_id) DO UPDATE SET
        status = excluded.status,
        attempt = excluded.attempt,
        idempotency_key = excluded.idempotency_key,
        lease_id = excluded.lease_id,
        error = excluded.error,
        state_json = excluded.state_json,
        updated_at = excluded.updated_at
      """,
      (
        step.step_id,
        step.run_id,
        step.node_id,
        step.status.value,
        step.attempt,
        step.idempotency_key,
        step.lease_id,
        step.error,
        json.dumps(step.to_dict(), ensure_ascii=False, sort_keys=True),
        step.created_at.isoformat(),
        step.updated_at.isoformat(),
      ),
    )

  def get(self, step_id: str) -> NodeStepRecord | None:
    row = self._conn.execute(
      "SELECT state_json FROM node_steps WHERE step_id = ?",
      (step_id,),
    ).fetchone()
    if row is None:
      return None
    return NodeStepRecord.from_dict(json.loads(row["state_json"]))

  def list_by_run(self, run_id: str) -> list[NodeStepRecord]:
    rows = self._conn.execute(
      """
      SELECT state_json
      FROM node_steps
      WHERE run_id = ?
      ORDER BY created_at ASC, step_id ASC
      """,
      (run_id,),
    ).fetchall()
    return [NodeStepRecord.from_dict(json.loads(row["state_json"])) for row in rows]

  def get_by_idempotency_key(self, idempotency_key: str) -> NodeStepRecord | None:
    row = self._conn.execute(
      """
      SELECT state_json
      FROM node_steps
      WHERE idempotency_key = ?
      ORDER BY created_at DESC, step_id DESC
      LIMIT 1
      """,
      (idempotency_key,),
    ).fetchone()
    if row is None:
      return None
    return NodeStepRecord.from_dict(json.loads(row["state_json"]))
