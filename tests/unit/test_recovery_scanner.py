from datetime import timedelta
import unittest

from agent_kernel.domain import ArtifactRef, NodeStepRecord, NodeStepStatus, RecoveryStatus, RunState, RunStatus, RuntimeEvent, RuntimeEventType
from agent_kernel.domain.base import utc_now
from agent_kernel.domain.events import RuntimeEventType
from agent_kernel.persistence import UnitOfWork, connect_sqlite
from agent_kernel.runtime import RecoveryScanner, unit_of_work_factory


class RecoveryScannerTests(unittest.TestCase):
  def test_stale_running_step_is_rescheduled_and_recovery_job_is_recorded(self) -> None:
    conn = connect_sqlite()
    try:
      stale_at = utc_now() - timedelta(minutes=10)
      with UnitOfWork(conn) as uow:
        uow.steps.save(
          NodeStepRecord(
            step_id="step_stale",
            run_id="run_recovery",
            node_id="node_1",
            status=NodeStepStatus.RUNNING,
            attempt=1,
            lease_id="lease_old",
            updated_at=stale_at,
          )
        )

      result = RecoveryScanner(unit_of_work_factory(conn), stale_after_seconds=60).scan()

      with UnitOfWork(conn) as uow:
        step = uow.steps.get("step_stale")
        jobs = uow.recovery.list_by_target("node_step", "step_stale")
        events = uow.events.list_by_run("run_recovery")

      self.assertEqual(result.scanned_steps, 1)
      self.assertEqual(result.recovered_steps, 1)
      self.assertEqual(step.status, NodeStepStatus.SCHEDULED)
      self.assertIsNone(step.lease_id)
      self.assertEqual(jobs[-1].status, RecoveryStatus.SUCCEEDED)
      self.assertIn(RuntimeEventType.RECOVERY_SUCCEEDED, [event.event_type for event in events])
    finally:
      conn.close()

  def test_stale_step_exhausting_recovery_attempts_is_dead_lettered(self) -> None:
    conn = connect_sqlite()
    try:
      stale_at = utc_now() - timedelta(minutes=10)
      with UnitOfWork(conn) as uow:
        uow.steps.save(
          NodeStepRecord(
            step_id="step_exhausted",
            run_id="run_recovery_dead",
            node_id="node_1",
            status=NodeStepStatus.LEASED,
            attempt=3,
            lease_id="lease_old",
            updated_at=stale_at,
          )
        )

      result = RecoveryScanner(
        unit_of_work_factory(conn),
        stale_after_seconds=60,
        max_recovery_attempts=3,
      ).scan()

      with UnitOfWork(conn) as uow:
        step = uow.steps.get("step_exhausted")
        jobs = uow.recovery.list_by_target("node_step", "step_exhausted")
        dead_letters = uow.dead_letters.list_all()
        events = uow.events.list_by_run("run_recovery_dead")

      self.assertEqual(result.scanned_steps, 1)
      self.assertEqual(result.dead_lettered_steps, 1)
      self.assertEqual(step.status, NodeStepStatus.FAILED)
      self.assertEqual(jobs[-1].status, RecoveryStatus.DEAD_LETTERED)
      self.assertEqual(dead_letters[0].failure_type, "recovery_exhausted")
      self.assertIn(RuntimeEventType.RECOVERY_FAILED, [event.event_type for event in events])
    finally:
      conn.close()

  def test_consistency_check_reports_missing_state_checkpoint_event_and_artifact(self) -> None:
    conn = connect_sqlite()
    try:
      missing_event_artifact = RuntimeEvent(
        event_type=RuntimeEventType.STEP_COMPLETED,
        run_id="run_missing_state",
        artifact_refs=[ArtifactRef(artifact_id="artifact_missing", uri="artifact://missing")],
      )
      state = RunState(
        run_id="run_bad_checkpoint",
        status=RunStatus.RUNNING,
        checkpoint_id="checkpoint_missing",
      )
      with UnitOfWork(conn) as uow:
        uow.events.append(missing_event_artifact)
        uow.states.save(state)
        uow.checkpoints.save(
          "run_bad_checkpoint",
          state,
          event_id="event_missing",
          checkpoint_id="checkpoint_current",
        )

      result = RecoveryScanner(unit_of_work_factory(conn)).check_consistency()

      issue_types = {issue.issue_type for issue in result.issues}
      self.assertIn("missing_run_state", issue_types)
      self.assertIn("artifact_ref_missing", issue_types)
      self.assertIn("checkpoint_event_missing", issue_types)
      self.assertIn("state_checkpoint_mismatch", issue_types)
      self.assertEqual(len(result.jobs), len(result.issues))
      self.assertTrue(all(job.status is RecoveryStatus.FAILED for job in result.jobs))

      with UnitOfWork(conn) as uow:
        jobs = uow.recovery.list_all()
        recovery_events = [
          event
          for event in uow.events.list_all()
          if event.event_type is RuntimeEventType.RECOVERY_FAILED
        ]

      self.assertEqual(len(jobs), len(result.issues))
      self.assertEqual(len(recovery_events), len(result.issues))
    finally:
      conn.close()

  def test_repair_consistency_restores_state_and_aligns_checkpoint(self) -> None:
    conn = connect_sqlite()
    try:
      created_event = RuntimeEvent(
        event_type=RuntimeEventType.RUN_CREATED,
        run_id="run_repair_missing_state",
      )
      checkpoint_state = RunState(
        run_id="run_repair_missing_state",
        status=RunStatus.RUNNING,
        checkpoint_id="checkpoint_restore",
      )
      old_state = RunState(
        run_id="run_repair_mismatch",
        status=RunStatus.RUNNING,
        checkpoint_id="checkpoint_old",
      )
      latest_state = RunState(
        run_id="run_repair_mismatch",
        status=RunStatus.RUNNING,
        checkpoint_id="checkpoint_latest",
      )
      with UnitOfWork(conn) as uow:
        uow.events.append(created_event)
        uow.checkpoints.save(
          "run_repair_missing_state",
          checkpoint_state,
          event_id=created_event.event_id,
          checkpoint_id="checkpoint_restore",
        )
        uow.states.save(old_state)
        uow.checkpoints.save(
          "run_repair_mismatch",
          old_state,
          checkpoint_id="checkpoint_old",
        )
        uow.checkpoints.save(
          "run_repair_mismatch",
          latest_state,
          checkpoint_id="checkpoint_latest",
        )

      result = RecoveryScanner(unit_of_work_factory(conn)).repair_consistency()

      with UnitOfWork(conn) as uow:
        restored = uow.states.get("run_repair_missing_state")
        aligned = uow.states.get("run_repair_mismatch")
        events = uow.events.list_all()

      self.assertEqual(result.repaired_issues, 2)
      self.assertEqual(result.unrepaired_issues, 0)
      self.assertIsNotNone(restored)
      self.assertEqual(restored.checkpoint_id, "checkpoint_restore")
      self.assertEqual(aligned.checkpoint_id, "checkpoint_latest")
      self.assertEqual(
        len([event for event in events if event.event_type is RuntimeEventType.RECOVERY_SUCCEEDED]),
        2,
      )
      self.assertEqual(
        RecoveryScanner(unit_of_work_factory(conn)).check_consistency().issues,
        [],
      )
    finally:
      conn.close()

  def test_repair_consistency_leaves_unrepairable_artifact_issue_failed(self) -> None:
    conn = connect_sqlite()
    try:
      event = RuntimeEvent(
        event_type=RuntimeEventType.STEP_COMPLETED,
        run_id="run_unrepairable",
        artifact_refs=[ArtifactRef(artifact_id="artifact_missing", uri="artifact://missing")],
      )
      state = RunState(run_id="run_unrepairable", status=RunStatus.RUNNING)
      with UnitOfWork(conn) as uow:
        uow.events.append(event)
        uow.states.save(state)

      result = RecoveryScanner(unit_of_work_factory(conn)).repair_consistency()

      self.assertEqual(result.repaired_issues, 0)
      self.assertEqual(result.unrepaired_issues, 1)
      self.assertEqual(result.jobs[0].status, RecoveryStatus.FAILED)
      self.assertEqual(result.issues[0].issue_type, "artifact_ref_missing")
    finally:
      conn.close()


if __name__ == "__main__":
  unittest.main()
