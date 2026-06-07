"""Simple in-process scheduler helpers."""

from dataclasses import replace

from agent_kernel.domain.base import utc_now
from agent_kernel.domain.states import NodeStepStatus, assert_transition
from agent_kernel.runtime.lease import Lease
from agent_kernel.domain.step import NodeStepRecord


class Scheduler:
  def lease_step(self, step: NodeStepRecord, owner_id: str, ttl_seconds: int = 60) -> NodeStepRecord:
    if step.status is NodeStepStatus.SCHEDULED:
      assert_transition(step.status, NodeStepStatus.LEASED)
    lease = Lease.create(owner_id=owner_id, ttl_seconds=ttl_seconds)
    return replace(
      step,
      status=NodeStepStatus.LEASED,
      lease_id=lease.lease_id,
      updated_at=utc_now(),
    )

  def mark_running(self, step: NodeStepRecord) -> NodeStepRecord:
    assert_transition(step.status, NodeStepStatus.RUNNING)
    return replace(step, status=NodeStepStatus.RUNNING, updated_at=utc_now())
