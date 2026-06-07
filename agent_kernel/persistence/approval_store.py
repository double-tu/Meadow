"""Approval request persistence."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

from agent_kernel.domain.base import utc_now
from agent_kernel.domain.policy import ApprovalRequest
from agent_kernel.domain.serialization import to_primitive
from agent_kernel.domain.states import ApprovalStatus


class ApprovalStore:
  def __init__(self, conn: sqlite3.Connection) -> None:
    self._conn = conn

  def save(self, request: ApprovalRequest) -> None:
    self._conn.execute(
      """
      INSERT INTO approval_requests (
        approval_id,
        run_id,
        target_type,
        target_id,
        status,
        request_json,
        created_at,
        resolved_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(approval_id) DO UPDATE SET
        status = excluded.status,
        request_json = excluded.request_json,
        resolved_at = excluded.resolved_at
      """,
      (
        request.approval_id,
        request.run_id,
        request.target_type,
        request.target_id,
        request.status.value,
        json.dumps(to_primitive(request), ensure_ascii=False, sort_keys=True),
        request.created_at.isoformat(),
        request.resolved_at.isoformat() if request.resolved_at else None,
      ),
    )

  def list_pending(self, run_id: str) -> list[ApprovalRequest]:
    rows = self._conn.execute(
      """
      SELECT request_json
      FROM approval_requests
      WHERE run_id = ?
        AND status = ?
      ORDER BY created_at ASC, approval_id ASC
      """,
      (run_id, ApprovalStatus.REQUESTED.value),
    ).fetchall()
    return [ApprovalRequest.from_dict(json.loads(row["request_json"])) for row in rows]

  def list_pending_all(self) -> list[ApprovalRequest]:
    rows = self._conn.execute(
      """
      SELECT request_json
      FROM approval_requests
      WHERE status = ?
      ORDER BY created_at ASC, approval_id ASC
      """,
      (ApprovalStatus.REQUESTED.value,),
    ).fetchall()
    return [ApprovalRequest.from_dict(json.loads(row["request_json"])) for row in rows]

  def get(self, approval_id: str) -> ApprovalRequest | None:
    row = self._conn.execute(
      "SELECT request_json FROM approval_requests WHERE approval_id = ?",
      (approval_id,),
    ).fetchone()
    if row is None:
      return None
    return ApprovalRequest.from_dict(json.loads(row["request_json"]))

  def resolve(self, approval_id: str, status: ApprovalStatus) -> ApprovalRequest:
    request = self.get(approval_id)
    if request is None:
      raise KeyError(f"Approval request not found: {approval_id}")
    request = replace(request, status=status, resolved_at=utc_now())
    self.save(request)
    return request
