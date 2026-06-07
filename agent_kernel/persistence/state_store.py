"""Run state persistence."""

from __future__ import annotations

import json
import sqlite3

from agent_kernel.domain.run import RunState
from agent_kernel.domain.serialization import to_primitive


class StateStore:
  def __init__(self, conn: sqlite3.Connection) -> None:
    self._conn = conn

  def save(self, state: RunState) -> None:
    self._conn.execute(
      """
      INSERT INTO run_states (run_id, state_json, updated_at)
      VALUES (?, ?, ?)
      ON CONFLICT(run_id) DO UPDATE SET
        state_json = excluded.state_json,
        updated_at = excluded.updated_at
      """,
      (
        state.run_id,
        json.dumps(to_primitive(state), ensure_ascii=False, sort_keys=True),
        state.updated_at.isoformat(),
      ),
    )

  def get(self, run_id: str) -> RunState | None:
    row = self._conn.execute(
      "SELECT state_json FROM run_states WHERE run_id = ?",
      (run_id,),
    ).fetchone()
    if row is None:
      return None
    return RunState.from_dict(json.loads(row["state_json"]))

