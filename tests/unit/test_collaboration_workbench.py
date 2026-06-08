import json
import unittest

from agent_kernel.agents import AgentDelegationBroker, ConnectorTurn, FakeAgentConnector
from agent_kernel.app.collaboration_workbench import CollaborationWorkbenchService
from agent_kernel.autonomy import SkillService
from agent_kernel.autonomy.builtin_skills import ensure_builtin_atomic_skills
from agent_kernel.domain import CollaborationWorkbenchKind, CollaborationWorkbenchStatus, ParticipantKind
from agent_kernel.hosts.http import HTTPHost, make_handler
from agent_kernel.persistence import UnitOfWork, connect_sqlite
from agent_kernel.runtime import unit_of_work_factory
from tests.unit.test_http_host import _dispatch_fake_request_with_headers


class CollaborationWorkbenchServiceTests(unittest.IsolatedAsyncioTestCase):
  async def test_builtin_workbench_skill_exposes_async_collaboration_tools(self) -> None:
    conn = connect_sqlite()
    try:
      skill_service = SkillService(unit_of_work_factory(conn))

      skills = ensure_builtin_atomic_skills(skill_service)
      workbench_skill = next(skill for skill in skills if skill.skill_id == "builtin.atomic.collaboration_workbench")

      self.assertIn("workbench_create", workbench_skill.recommended_tools)
      self.assertIn("workbench_message", workbench_skill.recommended_tools)
      self.assertIn("user_input_request", workbench_skill.recommended_tools)
      self.assertIn("持续任务", workbench_skill.when_to_use)
    finally:
      conn.close()

  async def test_group_chat_workbench_persists_members_messages_and_decision(self) -> None:
    conn = connect_sqlite()
    try:
      service = CollaborationWorkbenchService(unit_of_work_factory(conn))

      snapshot = service.create_group_chat(
        {
          "title": "架构群聊",
          "objective": "讨论多 Agent 工作台架构",
          "members": [
            {"participant_id": "human", "kind": "human", "role": "owner"},
            {"participant_id": "builder", "kind": "agent", "role": "builder"},
            {"participant_id": "reviewer", "kind": "agent", "role": "reviewer"},
          ],
        }
      )
      message = service.send_message(
        snapshot.workbench.workbench_id,
        {"sender_participant_id": "builder", "text": "建议用 interaction fabric 承载群聊。"},
      )
      decision = service.create_decision_artifact(
        snapshot.workbench.workbench_id,
        {"decided_by_participant_id": "human"},
      )
      refreshed = service.snapshot(snapshot.workbench.workbench_id)

      self.assertEqual(snapshot.workbench.kind, CollaborationWorkbenchKind.GROUP_CHAT)
      self.assertEqual(snapshot.workbench.status, CollaborationWorkbenchStatus.RUNNING)
      self.assertEqual(len(snapshot.members), 3)
      self.assertEqual(message.sender_participant_id, "builder")
      self.assertTrue(decision["ok"])
      self.assertEqual(refreshed.group_chat["status"], "completed")
      self.assertEqual(refreshed.messages[-1].content["type"], "decision_artifact")
    finally:
      conn.close()

  async def test_parallel_delegation_workbench_starts_child_agents(self) -> None:
    conn = connect_sqlite()
    try:
      connector_a = FakeAgentConnector()
      connector_b = FakeAgentConnector()
      connector_a.queue_response(
        ConnectorTurn("turn_a", "unused", {"summary": "searched docs"}, completed=True)
      )
      connector_b.queue_response(
        ConnectorTurn("turn_b", "unused", {"summary": "searched web"}, completed=True)
      )
      broker = AgentDelegationBroker(
        unit_of_work_factory(conn),
        {"codex-a": connector_a, "codex-b": connector_b},
      )
      service = CollaborationWorkbenchService(unit_of_work_factory(conn), delegation_control=broker)

      snapshot = await service.create_parallel_delegation(
        {
          "title": "并行搜索",
          "objective": "十秒内覆盖大量网页并汇总结论",
          "auto_start": True,
          "members": [
            {"role": "docs-search", "kind": "remote_agent", "connector_id": "codex-a", "agent_type": "codex"},
            {"role": "web-search", "kind": "remote_agent", "connector_id": "codex-b", "agent_type": "codex"},
          ],
          "slices": [
            {"title": "查官方文档", "objective": "搜索官方文档并提取事实"},
            {"title": "查网页资料", "objective": "搜索网页资料并提取事实"},
          ],
        }
      )
      for task_id in snapshot.workbench.delegation_task_ids:
        await broker.await_task(task_id)
      refreshed = service.snapshot(snapshot.workbench.workbench_id)

      self.assertEqual(snapshot.workbench.kind, CollaborationWorkbenchKind.PARALLEL_DELEGATION)
      self.assertEqual(len(snapshot.task_slices), 2)
      self.assertEqual(len(snapshot.workbench.delegation_task_ids), 2)
      self.assertEqual(refreshed.delegations[0]["status"], "completed")
      self.assertEqual(connector_a.messages[0].content["metadata"]["workbench_id"], snapshot.workbench.workbench_id)
      self.assertEqual(connector_b.messages[0].content["task"], "搜索网页资料并提取事实")
    finally:
      conn.close()

  async def test_technical_review_workbench_creates_review_slices_without_auto_start(self) -> None:
    conn = connect_sqlite()
    try:
      service = CollaborationWorkbenchService(unit_of_work_factory(conn))

      snapshot = await service.create_technical_review(
        {
          "title": "代码评审",
          "target_ref": "git://HEAD",
          "objective": "评审当前改动",
          "members": [
            {"participant_id": "reviewer-a", "kind": "agent", "role": "reviewer-a"},
            {"participant_id": "reviewer-b", "kind": "agent", "role": "reviewer-b"},
          ],
        }
      )

      self.assertEqual(snapshot.workbench.kind, CollaborationWorkbenchKind.TECHNICAL_REVIEW)
      self.assertEqual(snapshot.channel.mode, "review_queue")
      self.assertEqual(len(snapshot.task_slices), 4)
      self.assertEqual({item.status for item in snapshot.task_slices}, {"review"})
      self.assertEqual(snapshot.workbench.metadata["target_ref"], "git://HEAD")
      self.assertEqual(snapshot.workbench.delegation_task_ids, [])
    finally:
      conn.close()


class CollaborationWorkbenchHTTPTests(unittest.TestCase):
  def test_http_creates_and_inspects_group_chat_workbench(self) -> None:
    conn = connect_sqlite()
    try:
      handler = make_handler(HTTPHost(unit_of_work_factory(conn)))

      status, _headers, body = _dispatch_fake_request_with_headers(
        handler,
        "POST",
        "/collaboration/workbenches/group-chat",
        {
          "title": "HTTP 群聊",
          "objective": "验证工作台 API",
          "members": [
            {"participant_id": "human", "kind": "human", "role": "owner"},
            {"participant_id": "agent", "kind": "agent", "role": "assistant"},
          ],
        },
      )
      created = json.loads(body.decode("utf-8"))
      workbench_id = created["workbench"]["workbench"]["workbench_id"]
      message_status, _message_headers, message_body = _dispatch_fake_request_with_headers(
        handler,
        "POST",
        f"/collaboration/workbenches/{workbench_id}/messages",
        {"sender_participant_id": "agent", "text": "收到。"},
      )
      inspected_status, _inspect_headers, inspected_body = _dispatch_fake_request_with_headers(
        handler,
        "GET",
        f"/collaboration/workbenches/{workbench_id}",
      )
      workspace_payload = json.loads(
        _dispatch_fake_request_with_headers(handler, "GET", "/workspaces")[2].decode("utf-8")
      )
      message = json.loads(message_body.decode("utf-8"))
      inspected = json.loads(inspected_body.decode("utf-8"))

      self.assertIn("201 Created", status)
      self.assertIn("201 Created", message_status)
      self.assertIn("200 OK", inspected_status)
      self.assertEqual(created["workbench"]["workbench"]["kind"], "group_chat")
      self.assertEqual(message["message"]["content"]["text"], "收到。")
      self.assertEqual(len(inspected["workbench"]["messages"]), 1)
      self.assertEqual(workspace_payload["workspaces"][0]["collaboration_workbench_ids"], [workbench_id])
    finally:
      conn.close()

  def test_http_parallel_delegation_requires_broker_when_auto_starting(self) -> None:
    conn = connect_sqlite()
    try:
      handler = make_handler(HTTPHost(unit_of_work_factory(conn)))

      status, _headers, body = _dispatch_fake_request_with_headers(
        handler,
        "POST",
        "/collaboration/workbenches/parallel-delegation",
        {
          "objective": "需要真实子 Agent",
          "auto_start": True,
          "members": [
            {"role": "worker", "kind": "remote_agent", "connector_id": "missing", "agent_type": "codex"},
          ],
        },
      )
      payload = json.loads(body.decode("utf-8"))

      self.assertIn("400 Bad Request", status)
      self.assertFalse(payload["ok"])
      self.assertIn("delegation broker", payload["error"])
    finally:
      conn.close()


if __name__ == "__main__":
  unittest.main()
