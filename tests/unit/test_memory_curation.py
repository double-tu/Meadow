import unittest

from agent_kernel.app.conversation_history import ConversationHistoryCompactor
from agent_kernel.app.memory_curator import MemoryCurator
from agent_kernel.domain import RuntimeEvent, RuntimeEventType
from agent_kernel.domain.base import new_id
from agent_kernel.memory import MemoryFacade
from agent_kernel.persistence import UnitOfWork, connect_sqlite
from agent_kernel.runtime import unit_of_work_factory


class _Message:
  def __init__(self, role: str, content: str) -> None:
    self.message_id = new_id("msg")
    self.role = role
    self.content = content


class MemoryCurationTests(unittest.TestCase):
  def test_conversation_history_compactor_writes_episodic_summary(self) -> None:
    conn = connect_sqlite()
    try:
      memory = MemoryFacade(unit_of_work_factory(conn))
      compactor = ConversationHistoryCompactor(memory, threshold_messages=6, keep_recent_messages=2)
      messages = [
        _Message("user", f"question {index}") if index % 2 == 0 else _Message("assistant", f"answer {index}")
        for index in range(8)
      ]

      result = compactor.compact(scope="chat_compact", messages=messages)

      self.assertIsNotNone(result.memory)
      self.assertEqual(len(result.compacted_message_ids), 6)
      self.assertEqual(result.memory.content["kind"], "conversation_history_summary")
      self.assertEqual(result.memory.content["message_ids"], result.compacted_message_ids)
    finally:
      conn.close()

  def test_memory_curator_writes_run_episode_and_settles_candidates_once(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      with UnitOfWork(conn) as uow:
        uow.events.append(
          RuntimeEvent(
            event_type=RuntimeEventType.TOOL_CALL_COMPLETED,
            run_id="run_curate",
            payload={"summary": "browser_scan found Shenzhen weather"},
          )
        )
        uow.events.append(
          RuntimeEvent(
            event_type=RuntimeEventType.MEMORY_EVOLUTION_CANDIDATE,
            run_id="run_curate",
            payload={
              "candidate_id": "candidate_weather_pref",
              "scope": "chat_curate",
              "note": "User prefers weather answers in Chinese.",
              "evidence_summary": "TOOL_CALL_COMPLETED event reported browser_scan found Shenzhen weather.",
            },
          )
        )
      curator = MemoryCurator(uow_factory)

      first = curator.curate_run("run_curate", scope="chat_curate")
      second = curator.curate_run("run_curate", scope="chat_curate")
      memory = MemoryFacade(uow_factory)
      episodes = memory.retrieve("chat_curate", memory_type="episodic", limit=20)
      semantics = memory.retrieve("chat_curate", memory_type="semantic", limit=20)

      self.assertIsNotNone(first.episodic_memory_id)
      self.assertEqual(second.episodic_memory_id, None)
      self.assertEqual(len(first.settlements), 1)
      self.assertEqual(len(second.settlements), 0)
      self.assertEqual(len([item for item in episodes if item.content.get("kind") == "run_event_summary"]), 1)
      self.assertEqual(semantics[0].content["candidate_id"], "candidate_weather_pref")
    finally:
      conn.close()


if __name__ == "__main__":
  unittest.main()
