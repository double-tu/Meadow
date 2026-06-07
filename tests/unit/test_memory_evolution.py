import unittest

from agent_kernel.domain import RuntimeEvent, RuntimeEventType
from agent_kernel.memory import MemoryEvolutionSettlementService
from agent_kernel.persistence import UnitOfWork, connect_sqlite
from agent_kernel.runtime import unit_of_work_factory


class MemoryEvolutionSettlementTests(unittest.TestCase):
  def test_settle_run_writes_semantic_and_procedural_memory_idempotently(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      with UnitOfWork(conn) as uow:
        uow.events.append(
          RuntimeEvent(
            event_type=RuntimeEventType.MEMORY_EVOLUTION_CANDIDATE,
            run_id="run_memory",
            payload={
              "candidate_id": "candidate_fact",
              "scope": "project",
              "note": "User prefers concise status updates.",
            },
          )
        )
        uow.events.append(
          RuntimeEvent(
            event_type=RuntimeEventType.MEMORY_EVOLUTION_CANDIDATE,
            run_id="run_memory",
            payload={
              "candidate_id": "candidate_skill",
              "scope": "project",
              "note": "Workflow: inspect code, patch, then run focused tests.",
            },
          )
        )

      service = MemoryEvolutionSettlementService(uow_factory)
      settlements = service.settle_run("run_memory")
      repeated = service.settle_run("run_memory")

      with UnitOfWork(conn) as uow:
        semantic = uow.memory.list_by_scope("project", memory_type="semantic")
        procedural = uow.memory.list_by_scope("project", memory_type="procedural")
        events = uow.events.list_by_run("run_memory")

      self.assertEqual(len(settlements), 2)
      self.assertEqual(repeated, [])
      self.assertEqual(semantic[0].content["candidate_id"], "candidate_fact")
      self.assertEqual(procedural[0].content["candidate_id"], "candidate_skill")
      self.assertEqual(semantic[0].created_by, "memory_evolution_settlement")
      self.assertIn(RuntimeEventType.MEMORY_WRITE, [event.event_type for event in events])
    finally:
      conn.close()


if __name__ == "__main__":
  unittest.main()
