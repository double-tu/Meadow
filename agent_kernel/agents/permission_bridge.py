"""Permission bridge for delegated agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from agent_kernel.domain.base import new_id, utc_now


class PermissionBridgeStatus(StrEnum):
  REQUESTED = "requested"
  APPROVED = "approved"
  REJECTED = "rejected"
  CANCELLED = "cancelled"


@dataclass(slots=True)
class DelegatedPermissionRequest:
  parent_run_id: str
  child_session_id: str
  capability_id: str
  input_preview: dict[str, Any]
  reason: str
  request_id: str = field(default_factory=lambda: new_id("delegated_perm"))
  status: PermissionBridgeStatus = PermissionBridgeStatus.REQUESTED
  created_at: str = field(default_factory=lambda: utc_now().isoformat())

  def to_parent_message(self) -> dict[str, Any]:
    return {
      "type": "permission_request",
      "request_id": self.request_id,
      "parent_run_id": self.parent_run_id,
      "child_session_id": self.child_session_id,
      "capability_id": self.capability_id,
      "input_preview": self.input_preview,
      "reason": self.reason,
      "status": self.status.value,
    }


class LeaderPermissionBridge:
  """Creates auditable permission relay payloads for child agents."""

  def request(
    self,
    *,
    parent_run_id: str,
    child_session_id: str,
    capability_id: str,
    input_preview: dict[str, Any],
    reason: str,
  ) -> DelegatedPermissionRequest:
    return DelegatedPermissionRequest(
      parent_run_id=parent_run_id,
      child_session_id=child_session_id,
      capability_id=capability_id,
      input_preview=input_preview,
      reason=reason,
    )

  def response(self, request: DelegatedPermissionRequest, *, approved: bool, reason: str | None = None) -> dict[str, Any]:
    request.status = PermissionBridgeStatus.APPROVED if approved else PermissionBridgeStatus.REJECTED
    return {
      "type": "permission_response",
      "request_id": request.request_id,
      "approved": approved,
      "reason": reason,
      "status": request.status.value,
    }
