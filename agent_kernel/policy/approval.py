"""Approval service."""

from datetime import timedelta

from agent_kernel.domain.base import new_id, utc_now
from agent_kernel.domain.capability import CapabilityGrant
from agent_kernel.domain.policy import ApprovalRequest
from agent_kernel.domain.states import ApprovalStatus


class ApprovalService:
  def __init__(self, uow_factory) -> None:
    self._uow_factory = uow_factory

  def request_tool_approval(
    self,
    run_id: str,
    capability_id: str,
    reason: str | None,
    payload: dict[str, object],
  ) -> ApprovalRequest:
    request = ApprovalRequest(
      approval_id=new_id("approval"),
      run_id=run_id,
      target_type="tool_call",
      target_id=capability_id,
      reason=reason or "Approval required.",
      requested_payload=payload,
    )
    with self._uow_factory() as uow:
      uow.approvals.save(request)
    return request

  def approve(self, approval_id: str, ttl_seconds: int = 300) -> CapabilityGrant:
    with self._uow_factory() as uow:
      request = uow.approvals.resolve(approval_id, ApprovalStatus.APPROVED)
      grant = CapabilityGrant(
        grant_id=new_id("grant"),
        capability_id=request.target_id,
        run_id=request.run_id,
        expires_at=utc_now() + timedelta(seconds=ttl_seconds),
        approval_required=False,
      )
      uow.grants.save(grant)
      return grant

  def reject(self, approval_id: str) -> ApprovalRequest:
    with self._uow_factory() as uow:
      return uow.approvals.resolve(approval_id, ApprovalStatus.REJECTED)
