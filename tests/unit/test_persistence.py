import tempfile
import unittest
from pathlib import Path

from agent_kernel.domain import ArtifactRef, RunState, RunStatus, RuntimeEvent, RuntimeEventType
from agent_kernel.persistence import UnitOfWork, connect_sqlite


class PersistenceTests(unittest.TestCase):
  def test_event_state_checkpoint_and_artifact_round_trip(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      conn = connect_sqlite(Path(tmp) / "kernel.sqlite")
      try:
        event = RuntimeEvent(
          event_type=RuntimeEventType.RUN_CREATED,
          run_id="run_1",
          payload={"workflow_id": "wf_1"},
        )
        state = RunState(run_id="run_1", status=RunStatus.PENDING, workflow_id="wf_1")
        artifact = ArtifactRef(artifact_id="art_1", uri="artifact://art_1")

        with UnitOfWork(conn) as uow:
          uow.events.append(event)
          uow.states.save(state)
          checkpoint = uow.checkpoints.save("run_1", state, event_id=event.event_id)
          uow.artifacts.save(artifact, metadata={"kind": "test"})

        with UnitOfWork(conn) as uow:
          restored_events = uow.events.list_by_run("run_1")
          restored_state = uow.states.get("run_1")
          restored_checkpoint = uow.checkpoints.latest_for_run("run_1")
          restored_artifact = uow.artifacts.get("art_1")
      finally:
        conn.close()

      self.assertEqual(restored_events[0].event_type, RuntimeEventType.RUN_CREATED)
      self.assertIsNotNone(restored_state)
      self.assertEqual(restored_state.status, RunStatus.PENDING)
      self.assertIsNotNone(restored_checkpoint)
      self.assertEqual(restored_checkpoint.checkpoint_id, checkpoint.checkpoint_id)
      self.assertIsNotNone(restored_artifact)
      self.assertEqual(restored_artifact.uri, "artifact://art_1")

  def test_unit_of_work_rolls_back_on_error(self) -> None:
    conn = connect_sqlite()
    try:
      event = RuntimeEvent(event_type=RuntimeEventType.RUN_CREATED, run_id="run_rollback")

      with UnitOfWork(conn) as uow:
        pass

      with self.assertRaises(RuntimeError):
        with UnitOfWork(conn) as uow:
          uow.events.append(event)
          raise RuntimeError("boom")

      with UnitOfWork(conn) as uow:
        self.assertEqual(uow.events.list_by_run("run_rollback"), [])
    finally:
      conn.close()


if __name__ == "__main__":
  unittest.main()
