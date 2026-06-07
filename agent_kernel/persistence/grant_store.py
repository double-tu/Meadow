"""Capability grant persistence."""

from __future__ import annotations

import json
import sqlite3

from agent_kernel.domain.capability import CapabilityGrant
from agent_kernel.domain.serialization import to_primitive


class GrantStore:
  def __init__(self, conn: sqlite3.Connection) -> None:
    self._conn = conn

  def save(self, grant: CapabilityGrant) -> None:
    self._conn.execute(
      """
      INSERT INTO capability_grants (
        grant_id,
        capability_id,
        agent_id,
        task_id,
        run_id,
        approval_required,
        grant_json,
        expires_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(grant_id) DO UPDATE SET
        approval_required = excluded.approval_required,
        grant_json = excluded.grant_json,
        expires_at = excluded.expires_at
      """,
      (
        grant.grant_id,
        grant.capability_id,
        grant.agent_id,
        grant.task_id,
        grant.run_id,
        1 if grant.approval_required else 0,
        json.dumps(to_primitive(grant), ensure_ascii=False, sort_keys=True),
        grant.expires_at.isoformat(),
      ),
    )

  def list_for_run(self, run_id: str) -> list[CapabilityGrant]:
    rows = self._conn.execute(
      """
      SELECT grant_json
      FROM capability_grants
      WHERE run_id = ?
      ORDER BY expires_at DESC, grant_id ASC
      """,
      (run_id,),
    ).fetchall()
    return [CapabilityGrant.from_dict(json.loads(row["grant_json"])) for row in rows]

