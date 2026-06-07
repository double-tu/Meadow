import unittest

from agent_kernel.domain import NodeStepRecord, NodeStepStatus, RecoveryJob, RecoveryStatus
from agent_kernel.domain.stability import DeadLetterItem
from agent_kernel.persistence import OutboxItem, UnitOfWork, connect_sqlite


class ReliabilityPersistenceTests(unittest.TestCase):
  def test_step_dead_letter_and_outbox_round_trip(self) -> None:
    conn = connect_sqlite()
    try:
      step = NodeStepRecord(
        step_id="step_1",
        run_id="run_1",
        node_id="node_1",
        status=NodeStepStatus.SCHEDULED,
        idempotency_key="run_1:node_1:1",
      )
      dead = DeadLetterItem(
        item_id="dead_1",
        target_type="node_step",
        target_id="step_1",
        reason="invalid input",
        failure_type="deterministic",
      )
      outbox = OutboxItem.create(topic="run.updated", payload={"run_id": "run_1"})
      recovery = RecoveryJob(
        recovery_id="recovery_1",
        target_type="node_step",
        target_id="step_1",
        status=RecoveryStatus.PENDING,
        reason="stale step",
      )

      with UnitOfWork(conn) as uow:
        uow.steps.save(step)
        uow.dead_letters.add(dead)
        uow.outbox.add(outbox)
        uow.recovery.save(recovery)

      with UnitOfWork(conn) as uow:
        restored_step = uow.steps.get("step_1")
        restored_by_key = uow.steps.get_by_idempotency_key("run_1:node_1:1")
        restored_dead = uow.dead_letters.list_all()
        restored_outbox = uow.outbox.list_pending()
        restored_recovery = uow.recovery.get("recovery_1")

      self.assertIsNotNone(restored_step)
      self.assertEqual(restored_step.status, NodeStepStatus.SCHEDULED)
      self.assertIsNotNone(restored_by_key)
      self.assertEqual(restored_by_key.step_id, "step_1")
      self.assertEqual(restored_dead[0].failure_type, "deterministic")
      self.assertEqual(restored_outbox[0].payload["run_id"], "run_1")
      self.assertEqual(restored_recovery.status, RecoveryStatus.PENDING)
    finally:
      conn.close()


if __name__ == "__main__":
  unittest.main()
