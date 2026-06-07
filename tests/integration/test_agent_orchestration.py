import unittest

from agent_kernel.agents import (
  AgentLoop,
  AgentSessionService,
  SkillContextProvider,
  SupervisorService,
  build_mailbox_message,
  oneshot_agent_spec,
)
from agent_kernel.autonomy import SkillService
from agent_kernel.domain import AgentStatus, RuntimeEventType
from agent_kernel.models import MockModelProvider, ModelGateway
from agent_kernel.persistence import UnitOfWork, connect_sqlite
from agent_kernel.runtime import unit_of_work_factory


class AgentOrchestrationIntegrationTests(unittest.IsolatedAsyncioTestCase):
  async def test_supervisor_spawns_child_and_child_completes_turn(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      sessions = AgentSessionService(uow_factory)
      supervisor = SupervisorService(uow_factory, sessions)
      parent = sessions.create_session(oneshot_agent_spec("parent", "mock-small"))
      child_spec = oneshot_agent_spec("child", "mock-small")
      child = supervisor.spawn_child(parent.session_id, child_spec, {"task_id": "task_1", "title": "do it"})

      gateway = ModelGateway()
      provider = MockModelProvider(responses=[{"finish": True, "output": {"summary": "done"}}])
      gateway.register_provider("mock", provider)
      loop = AgentLoop(uow_factory, gateway)

      turn = await loop.run_once(child.session_id, model_ref="mock-small")

      self.assertEqual(turn.session.status, AgentStatus.COMPLETED)
      self.assertIsNotNone(turn.result)
      self.assertEqual(turn.result.output, {"summary": "done"})
      self.assertEqual(supervisor.get_child_result(child.session_id).output, {"summary": "done"})
      self.assertEqual(supervisor.await_child(child.session_id), AgentStatus.COMPLETED)
      self.assertEqual(provider.calls[0][1].messages[0]["content"]["type"], "task.assigned")

      with UnitOfWork(conn) as uow:
        events = uow.events.list_by_run(f"agent:{child.session_id}")
        pending = uow.mailbox.list_pending(child.session_id)

      self.assertEqual(pending, [])
      self.assertEqual(events[0].event_type, RuntimeEventType.AGENT_SESSION_CREATED)
      self.assertEqual(events[1].event_type, RuntimeEventType.AGENT_TURN_COMPLETED)
      self.assertEqual(events[1].payload["output"], {"summary": "done"})
    finally:
      conn.close()

  async def test_supervisor_can_cancel_child(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      sessions = AgentSessionService(uow_factory)
      supervisor = SupervisorService(uow_factory, sessions)
      parent = sessions.create_session(oneshot_agent_spec("parent", "mock-small"))
      child = supervisor.spawn_child(
        parent.session_id,
        oneshot_agent_spec("child", "mock-small"),
        {"task_id": "task_2"},
      )

      cancelled = supervisor.cancel_child(child.session_id)

      self.assertEqual(cancelled.status, AgentStatus.CANCELLED)
      self.assertEqual(supervisor.await_child(child.session_id), AgentStatus.CANCELLED)

      with UnitOfWork(conn) as uow:
        events = uow.events.list_by_run(f"agent:{child.session_id}")

      self.assertIn(RuntimeEventType.AGENT_SESSION_CANCELLED, [event.event_type for event in events])
    finally:
      conn.close()

  async def test_supervisor_can_cascade_cancel_children(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      sessions = AgentSessionService(uow_factory)
      supervisor = SupervisorService(uow_factory, sessions)
      parent = sessions.create_session(oneshot_agent_spec("parent", "mock-small"))
      child_a = supervisor.spawn_child(parent.session_id, oneshot_agent_spec("child-a", "mock-small"), {})
      child_b = supervisor.spawn_child(parent.session_id, oneshot_agent_spec("child-b", "mock-small"), {})

      cancelled = supervisor.cancel_children(parent.session_id)

      self.assertEqual([session.status for session in cancelled], [AgentStatus.CANCELLED, AgentStatus.CANCELLED])
      self.assertEqual(supervisor.await_child(child_a.session_id), AgentStatus.CANCELLED)
      self.assertEqual(supervisor.await_child(child_b.session_id), AgentStatus.CANCELLED)
    finally:
      conn.close()

  async def test_agent_loop_parses_structured_execution_command(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      sessions = AgentSessionService(uow_factory)
      session = sessions.create_session(oneshot_agent_spec("agent", "mock-small"))
      gateway = ModelGateway()
      provider = MockModelProvider(
        responses=[
          {
            "finish": False,
            "output": {"note": "need approval"},
            "command": {
              "type": "request_approval",
              "target": "human",
              "payload": {"reason": "confirm"},
            },
          }
        ]
      )
      gateway.register_provider("mock", provider)
      loop = AgentLoop(uow_factory, gateway)

      turn = await loop.run_once(session.session_id, model_ref="mock-small")

      self.assertEqual(turn.session.status, AgentStatus.IDLE)
      self.assertIsNotNone(turn.command)
      self.assertEqual(turn.command.type, "request_approval")
      self.assertEqual(turn.command.payload["reason"], "confirm")
    finally:
      conn.close()

  async def test_agent_loop_parses_json_content_from_real_provider_shape(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      sessions = AgentSessionService(uow_factory)
      session = sessions.create_session(oneshot_agent_spec("agent-json", "mock-small"))
      gateway = ModelGateway()
      provider = MockModelProvider(
        responses=[
          {
            "content": '{"finish": true, "output": {"summary": "json ok"}}',
            "usage": {"total_tokens": 4},
          }
        ]
      )
      gateway.register_provider("mock", provider)
      loop = AgentLoop(uow_factory, gateway)

      turn = await loop.run_once(session.session_id, model_ref="mock-small")

      self.assertEqual(turn.session.status, AgentStatus.COMPLETED)
      self.assertIsNotNone(turn.result)
      assert turn.result is not None
      self.assertEqual(turn.result.output["summary"], "json ok")
    finally:
      conn.close()

  async def test_agent_loop_injects_selected_skills_into_model_context(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      sessions = AgentSessionService(uow_factory)
      session = sessions.create_session(oneshot_agent_spec("agent-skill", "mock-small"))
      skill_service = SkillService(uow_factory)
      skill = skill_service.create_interpreted_skill(
        name="browser-check",
        description="Inspect browser automation failures",
        when_to_use="browser workflow automation",
        instructions="Inspect browser sessions before changing state.",
        recommended_tools=["control.browser.inspect"],
      )
      skill_service.activate(skill.skill_id)
      with UnitOfWork(conn) as uow:
        uow.mailbox.send(
          build_mailbox_message(
            recipient_session_id=session.session_id,
            content={"task": "fix browser workflow automation"},
            sender_session_id="user",
          )
        )
      gateway = ModelGateway()
      provider = MockModelProvider(responses=[{"finish": True, "output": {"summary": "used skill"}}])
      gateway.register_provider("mock", provider)
      loop = AgentLoop(
        uow_factory,
        gateway,
        skill_context_provider=SkillContextProvider(skill_service),
      )

      turn = await loop.run_once(session.session_id, model_ref="mock-small")

      with UnitOfWork(conn) as uow:
        events = uow.events.list_by_run(f"agent:{session.session_id}")

      self.assertEqual(turn.session.status, AgentStatus.COMPLETED)
      self.assertEqual(provider.calls[0][1].messages[0]["content"]["type"], "selected_skills")
      self.assertEqual(provider.calls[0][1].messages[0]["content"]["skills"][0]["skill_id"], skill.skill_id)
      self.assertEqual(events[-1].payload["selected_skill_ids"], [skill.skill_id])
    finally:
      conn.close()


if __name__ == "__main__":
  unittest.main()
