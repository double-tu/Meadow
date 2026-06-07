import unittest

from agent_kernel.domain import ArtifactRef, RunState, RunStatus
from agent_kernel.domain.events import RuntimeEvent, RuntimeEventType
from agent_kernel.observability import ArtifactInspectionService, CostService, TraceService
from agent_kernel.persistence import UnitOfWork, connect_sqlite
from agent_kernel.runtime import unit_of_work_factory


class ObservabilityReplayUnitTests(unittest.TestCase):
  def test_cost_ledger_aggregates_usage_payloads(self) -> None:
    conn = connect_sqlite()
    try:
      with UnitOfWork(conn) as uow:
        uow.events.append(
          RuntimeEvent(
            event_type=RuntimeEventType.MODEL_CALL_COMPLETED,
            run_id="run_cost",
            payload={"usage": {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01}},
          )
        )

      ledger = CostService(unit_of_work_factory(conn)).build_ledger("run_cost")

      self.assertEqual(ledger.model_calls, 1)
      self.assertEqual(ledger.input_tokens, 10)
      self.assertEqual(ledger.output_tokens, 5)
      self.assertEqual(ledger.cost_usd, 0.01)
    finally:
      conn.close()

  def test_trace_timeline_includes_events(self) -> None:
    conn = connect_sqlite()
    try:
      with UnitOfWork(conn) as uow:
        uow.events.append(RuntimeEvent(event_type=RuntimeEventType.RUN_CREATED, run_id="run_trace"))

      timeline = TraceService(unit_of_work_factory(conn)).build_timeline("run_trace")

      self.assertEqual(timeline.run_id, "run_trace")
      self.assertEqual(timeline.entries[0].kind, "event")
      self.assertEqual(timeline.entries[0].summary, "run.created")
    finally:
      conn.close()

  def test_artifact_inspection_aggregates_state_and_event_refs(self) -> None:
    conn = connect_sqlite()
    try:
      state_ref = ArtifactRef(artifact_id="art_state", uri="artifact://state")
      event_ref = ArtifactRef(artifact_id="art_event", uri="artifact://event")
      with UnitOfWork(conn) as uow:
        uow.artifacts.save(state_ref)
        uow.states.save(
          RunState(run_id="run_art", status=RunStatus.COMPLETED, artifact_refs=[state_ref])
        )
        uow.events.append(
          RuntimeEvent(
            event_type=RuntimeEventType.ARTIFACT_CREATED,
            run_id="run_art",
            artifact_refs=[event_ref],
          )
        )

      inspection = ArtifactInspectionService(unit_of_work_factory(conn)).inspect_run("run_art")

      self.assertEqual([ref.artifact_id for ref in inspection.artifact_refs], ["art_state"])
      self.assertEqual(inspection.missing_artifact_ids, ["art_event"])
    finally:
      conn.close()


if __name__ == "__main__":
  unittest.main()
