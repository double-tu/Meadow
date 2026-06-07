import unittest

from agent_kernel.agents import AgentLoop, AgentSessionService, build_mailbox_message, oneshot_agent_spec
from agent_kernel.context import ContextManager
from agent_kernel.memory import MemoryFacade
from agent_kernel.models import MockModelProvider, ModelGateway
from agent_kernel.persistence import UnitOfWork, connect_sqlite
from agent_kernel.runtime import unit_of_work_factory


class AgentContextIntegrationTests(unittest.IsolatedAsyncioTestCase):
  async def test_agent_loop_uses_context_manager_memory(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      sessions = AgentSessionService(uow_factory)
      session = sessions.create_session(oneshot_agent_spec("agent", "mock-small"))
      memory = MemoryFacade(uow_factory)
      memory.write_working(
        scope=session.session_id,
        content={"constraint": "respond with summary"},
        importance=1.0,
      )
      with UnitOfWork(conn) as uow:
        uow.mailbox.send(
          build_mailbox_message(
            recipient_session_id=session.session_id,
            mailbox_id=session.mailbox_id,
            content={"type": "task.assigned", "task": "summarize"},
          )
        )
      gateway = ModelGateway()
      provider = MockModelProvider(responses=[{"finish": True, "output": {"summary": "ok"}}])
      gateway.register_provider("mock", provider)
      context_manager = ContextManager(uow_factory, memory, max_tokens=512)
      loop = AgentLoop(uow_factory, gateway, context_manager=context_manager)

      await loop.run_once(session.session_id, model_ref="mock-small")

      model_context = provider.calls[0][1]
      self.assertGreaterEqual(len(model_context.memory_refs), 1)
      self.assertEqual(model_context.messages[-1]["content"]["content"]["constraint"], "respond with summary")

      with UnitOfWork(conn) as uow:
        events = uow.events.list_by_run(f"agent:{session.session_id}")

      self.assertEqual(events[1].event_type, "context.built")
    finally:
      conn.close()


if __name__ == "__main__":
  unittest.main()

