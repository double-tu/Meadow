from datetime import timedelta
import unittest

from agent_kernel.domain.base import utc_now
from agent_kernel.domain.capability import CapabilityGrant, CapabilitySpec, SideEffectLevel
from agent_kernel.domain.states import ApprovalStatus
from agent_kernel.persistence import UnitOfWork, connect_sqlite
from agent_kernel.policy import ApprovalService, PolicyDecisionType, PolicyEngine
from agent_kernel.runtime import unit_of_work_factory


class ApprovalAndGrantTests(unittest.TestCase):
  def test_approve_creates_run_scoped_grant(self) -> None:
    conn = connect_sqlite()
    try:
      service = ApprovalService(unit_of_work_factory(conn))
      approval = service.request_tool_approval(
        run_id="run_1",
        capability_id="tool.exec",
        reason="dangerous",
        payload={},
      )

      grant = service.approve(approval.approval_id)

      with UnitOfWork(conn) as uow:
        resolved = uow.approvals.get(approval.approval_id)
        grants = uow.grants.list_for_run("run_1")

      self.assertEqual(resolved.status, ApprovalStatus.APPROVED)
      self.assertEqual(grants[0].grant_id, grant.grant_id)
      self.assertEqual(grants[0].capability_id, "tool.exec")
    finally:
      conn.close()

  def test_reject_marks_approval_rejected_without_grant(self) -> None:
    conn = connect_sqlite()
    try:
      service = ApprovalService(unit_of_work_factory(conn))
      approval = service.request_tool_approval(
        run_id="run_1",
        capability_id="tool.exec",
        reason="dangerous",
        payload={},
      )

      rejected = service.reject(approval.approval_id)

      with UnitOfWork(conn) as uow:
        grants = uow.grants.list_for_run("run_1")

      self.assertEqual(rejected.status, ApprovalStatus.REJECTED)
      self.assertEqual(grants, [])
    finally:
      conn.close()

  def test_policy_ignores_expired_grant(self) -> None:
    spec = CapabilitySpec(
      capability_id="tool.exec",
      name="exec",
      kind="tool",
      input_schema={},
      output_schema={},
      side_effect_level=SideEffectLevel.EXEC,
    )
    expired = CapabilityGrant(
      grant_id="grant_1",
      capability_id="tool.exec",
      run_id="run_1",
      expires_at=utc_now() - timedelta(seconds=1),
    )
    policy = PolicyEngine(grants=[expired])

    decision = policy.decide(spec, run_id="run_1")

    self.assertEqual(decision.type, PolicyDecisionType.REQUIRE_APPROVAL)


if __name__ == "__main__":
  unittest.main()

