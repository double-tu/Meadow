import unittest

from agent_kernel.context import ContextManager
from agent_kernel.domain import RuntimeEventType
from agent_kernel.memory import (
  HTTPVectorStore,
  HTTPVectorStoreEndpoint,
  InMemoryVectorStore,
  MemoryFacade,
  VectorStoreSemanticRetriever,
)
from agent_kernel.persistence import UnitOfWork, connect_sqlite
from agent_kernel.runtime import unit_of_work_factory


class MemoryContextTests(unittest.TestCase):
  def test_memory_facade_round_trip(self) -> None:
    conn = connect_sqlite()
    try:
      memory = MemoryFacade(unit_of_work_factory(conn))

      item = memory.write_working(
        scope="session_1",
        content={"constraint": "use sqlite"},
        importance=0.9,
      )
      restored = memory.retrieve("session_1", memory_type="working")

      self.assertEqual(restored[0].memory_id, item.memory_id)
      self.assertEqual(restored[0].content["constraint"], "use sqlite")
    finally:
      conn.close()

  def test_context_manager_selects_memory_and_writes_ledger(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      memory = MemoryFacade(uow_factory)
      important = memory.write_working(
        scope="session_1",
        content={"important": "keep"},
        importance=1.0,
      )
      memory.write_working(
        scope="session_1",
        content={"large": "x" * 1000},
        importance=0.1,
      )
      manager = ContextManager(uow_factory, memory, max_tokens=80)

      context = manager.build(
        run_id="run_ctx",
        scope="session_1",
        model_ref="mock-small",
        messages=[{"role": "user", "content": "task"}],
      )

      self.assertEqual(context.memory_refs[0].memory_id, important.memory_id)
      self.assertGreaterEqual(len(context.omitted_candidates), 1)
      self.assertIn(important.memory_id, context.inclusion_rationale)

      with UnitOfWork(conn) as uow:
        events = uow.events.list_by_run("run_ctx")

      self.assertEqual(events[0].event_type, RuntimeEventType.CONTEXT_BUILT)
      self.assertEqual(events[0].payload["context_plan"]["run_id"], "run_ctx")
    finally:
      conn.close()

  def test_context_manager_filters_sensitive_memory(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      memory = MemoryFacade(uow_factory)
      public = memory.write_working("session_1", {"fact": "visible"}, importance=1.0)
      secret = memory.write_working("session_1", {"secret": "hidden"}, importance=1.0)
      secret.sensitivity = "secret"
      with UnitOfWork(conn) as uow:
        uow.memory.save(secret)
      manager = ContextManager(uow_factory, memory, max_tokens=256)

      context = manager.build(
        run_id="run_sensitive",
        scope="session_1",
        model_ref="mock-small",
        messages=[],
        allowed_sensitivities={"public", "internal"},
      )

      self.assertEqual([ref.memory_id for ref in context.memory_refs], [public.memory_id])
    finally:
      conn.close()

  def test_context_manager_prunes_tool_visibility(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      memory = MemoryFacade(uow_factory)
      manager = ContextManager(uow_factory, memory, max_tokens=256)

      context = manager.build(
        run_id="run_tools",
        scope="session_1",
        model_ref="mock-small",
        messages=[],
        available_tools=[
          {"name": "tool.safe", "schema": {}},
          {"name": "tool.dangerous", "schema": {}},
        ],
        tool_allowlist={"tool.safe"},
      )

      self.assertEqual(context.tool_schemas, [{"name": "tool.safe", "schema": {}}])
      with UnitOfWork(conn) as uow:
        event = uow.events.list_by_run("run_tools")[0]
      self.assertIn("tool_visibility_pruned", event.payload["context_plan"]["quality_warnings"])
    finally:
      conn.close()

  def test_large_memory_content_becomes_artifact_ref(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      memory = MemoryFacade(uow_factory)
      item = memory.write_working(
        scope="session_1",
        content={"raw": "x" * 2000},
        importance=1.0,
      )
      manager = ContextManager(uow_factory, memory, max_tokens=512)

      context = manager.build(
        run_id="run_large",
        scope="session_1",
        model_ref="mock-small",
        messages=[],
      )

      self.assertEqual(item.content["summary"], "Large memory content stored as artifact reference.")
      self.assertEqual(len(item.source_artifact_refs), 1)
      self.assertEqual(context.attachments[0].artifact_id, item.source_artifact_refs[0].artifact_id)
    finally:
      conn.close()

  def test_episodic_memory_retrieval_and_consolidation(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      memory = MemoryFacade(uow_factory)
      episode = memory.write_episode(
        scope="project_1",
        task_id="task_api",
        event_ids=["evt_1", "evt_2"],
        observations=["Fixed sqlite checkpoint resume bug", "Added recovery tests"],
        outcome="runtime recovery passed",
        importance=0.95,
      )
      memory.write_episode(
        scope="project_1",
        task_id="task_ui",
        event_ids=["evt_3"],
        observations=["Updated button colors"],
        outcome="visual polish done",
        importance=0.2,
      )

      retrieved = memory.retrieve_episodic("project_1", "checkpoint recovery sqlite")
      semantic = memory.consolidate_episode("project_1", episode)

      self.assertEqual(retrieved[0].memory_id, episode.memory_id)
      self.assertEqual(episode.memory_type, "episodic")
      self.assertIn("sqlite", episode.content["keywords"])
      self.assertIsNotNone(semantic)
      self.assertEqual(semantic.memory_type, "semantic")
      self.assertEqual(semantic.content["source_episode_id"], episode.memory_id)
      self.assertEqual(semantic.source_event_ids, ["evt_1", "evt_2"])
    finally:
      conn.close()

  def test_low_importance_episode_is_not_consolidated(self) -> None:
    conn = connect_sqlite()
    try:
      memory = MemoryFacade(unit_of_work_factory(conn))
      episode = memory.write_episode(
        scope="project_1",
        task_id="task_minor",
        event_ids=[],
        observations=["Minor temporary note"],
        outcome="done",
        importance=0.1,
      )

      self.assertIsNone(memory.consolidate_episode("project_1", episode))
    finally:
      conn.close()

  def test_semantic_memory_retrieval_ranks_by_sparse_similarity(self) -> None:
    conn = connect_sqlite()
    try:
      memory = MemoryFacade(unit_of_work_factory(conn))
      runtime = memory.write_semantic(
        scope="project_1",
        content={
          "summary": "Runtime checkpoint recovery uses sqlite event log replay",
          "keywords": ["runtime", "checkpoint", "sqlite", "recovery"],
        },
        importance=0.7,
      )
      memory.write_semantic(
        scope="project_1",
        content={"summary": "Button color polish and layout spacing"},
        importance=1.0,
      )

      results = memory.retrieve_semantic("project_1", "sqlite checkpoint replay", limit=2)

      self.assertEqual(results[0].memory.memory_id, runtime.memory_id)
      self.assertGreater(results[0].score, 0)
      self.assertIn("Matched terms", results[0].rationale)
    finally:
      conn.close()

  def test_semantic_memory_retrieval_can_use_vector_store_backend(self) -> None:
    conn = connect_sqlite()
    try:
      vector_store = InMemoryVectorStore()
      memory = MemoryFacade(
        unit_of_work_factory(conn),
        semantic_retriever=VectorStoreSemanticRetriever(vector_store),
      )
      runtime = memory.write_semantic(
        scope="project_1",
        content={
          "summary": "Runtime checkpoint recovery uses sqlite event log replay",
          "keywords": ["runtime", "checkpoint", "sqlite", "recovery"],
        },
        importance=0.7,
      )
      memory.write_semantic(
        scope="project_1",
        content={"summary": "Button color polish and layout spacing"},
        importance=1.0,
      )

      results = memory.retrieve_semantic("project_1", "sqlite checkpoint replay", limit=2)

      self.assertEqual(results[0].memory.memory_id, runtime.memory_id)
      self.assertGreater(results[0].score, 0)
      self.assertIn("vector store", results[0].rationale)
    finally:
      conn.close()

  def test_semantic_memory_retrieval_can_use_http_vector_store_backend(self) -> None:
    conn = connect_sqlite()
    try:
      transport = _HTTPVectorStoreTransport()
      store = HTTPVectorStore(
        HTTPVectorStoreEndpoint(
          base_url="https://vector.example",
          headers={"Authorization": "Bearer test"},
        ),
        transport=transport.post,
      )
      memory = MemoryFacade(
        unit_of_work_factory(conn),
        semantic_retriever=VectorStoreSemanticRetriever(store),
      )
      runtime = memory.write_semantic(
        scope="project_1",
        content={"summary": "Runtime checkpoint recovery uses sqlite event log replay"},
        importance=0.7,
      )
      memory.write_semantic(
        scope="project_1",
        content={"summary": "Button color polish and layout spacing"},
        importance=1.0,
      )
      transport.match_id = runtime.memory_id

      results = memory.retrieve_semantic("project_1", "sqlite checkpoint replay", limit=1)

      self.assertEqual(results[0].memory.memory_id, runtime.memory_id)
      self.assertEqual(transport.requests[0][0], "https://vector.example/upsert")
      self.assertEqual(transport.requests[1][0], "https://vector.example/query")
      self.assertEqual(transport.requests[1][1]["scope"], "project_1")
      self.assertEqual(transport.requests[1][1]["filters"], {"memory_type": "semantic"})
    finally:
      conn.close()

  def test_fact_conflict_detection_finds_conflicting_semantic_values(self) -> None:
    conn = connect_sqlite()
    try:
      memory = MemoryFacade(unit_of_work_factory(conn))
      memory.write_semantic(
        scope="project_1",
        content={
          "facts": [
            {
              "subject": "runtime.storage",
              "predicate": "backend",
              "value": "sqlite",
            }
          ]
        },
        confidence=0.95,
      )
      incoming = memory.write_semantic(
        scope="project_1",
        content={
          "subject": "runtime.storage",
          "predicate": "backend",
          "value": "postgres",
        },
        confidence=0.9,
      )

      conflicts = memory.detect_fact_conflicts("project_1", incoming)

      self.assertEqual(len(conflicts), 1)
      self.assertEqual(conflicts[0].incoming.value, "postgres")
      self.assertEqual(conflicts[0].existing.value, "sqlite")
      self.assertEqual(conflicts[0].severity, "error")
    finally:
      conn.close()

  def test_write_semantic_can_attach_fact_conflict_metadata(self) -> None:
    conn = connect_sqlite()
    try:
      memory = MemoryFacade(unit_of_work_factory(conn))
      memory.write_semantic(
        scope="project_1",
        content={"subject": "agent.mode", "predicate": "default", "value": "autonomous"},
        confidence=0.9,
      )

      incoming = memory.write_semantic(
        scope="project_1",
        content={"subject": "agent.mode", "predicate": "default", "value": "manual"},
        confidence=0.9,
        check_conflicts=True,
      )

      self.assertEqual(incoming.content["conflicts"][0]["existing_value"], "autonomous")
      self.assertEqual(incoming.content["conflicts"][0]["incoming_value"], "manual")
    finally:
      conn.close()


class _HTTPVectorStoreTransport:
  def __init__(self) -> None:
    self.requests = []
    self.match_id = ""

  def post(self, url, payload, endpoint):
    self.requests.append((url, payload, dict(endpoint.headers)))
    if url.endswith("/query"):
      return {"matches": [{"document_id": self.match_id, "score": 0.91, "metadata": {"source": "http"}}]}
    return {"ok": True}


if __name__ == "__main__":
  unittest.main()
