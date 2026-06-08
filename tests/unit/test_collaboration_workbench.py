import json
import unittest

from agent_kernel.agents import AgentDelegationBroker, ConnectorTurn, FakeAgentConnector
from agent_kernel.app.collaboration_workbench import CollaborationWorkbenchService
from agent_kernel.app.orchestration_tools import OrchestrationCapabilityProvider
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

  async def test_builtin_web_research_skill_is_sop_driven_over_browser_atoms(self) -> None:
    conn = connect_sqlite()
    try:
      skill_service = SkillService(unit_of_work_factory(conn))

      skills = ensure_builtin_atomic_skills(skill_service)
      web_skill = next(skill for skill in skills if skill.skill_id == "builtin.atomic.web_research")

      self.assertIn("SOP", web_skill.instructions)
      self.assertIn("search_results", web_skill.instructions)
      self.assertIn("打开至少 2 个结果页", web_skill.instructions)
      self.assertIn("动态信息流", web_skill.instructions)
      self.assertIn("短 JSON 数组", web_skill.instructions)
      self.assertIn("browser_execute_js", web_skill.instructions)
      self.assertIn("browser_scan", web_skill.recommended_tools)
      self.assertIn("browser_navigate", web_skill.recommended_tools)
      self.assertIn("browser_execute_js", web_skill.recommended_tools)
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

  async def test_model_tool_degrades_auto_start_when_delegation_broker_missing(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      workbench_service = CollaborationWorkbenchService(uow_factory)
      provider = OrchestrationCapabilityProvider(
        uow_factory=uow_factory,
        task_launcher=_FakeTaskLauncher(),
        workbench_control=workbench_service,
      )

      result = await provider.create_workbench(
        {
          "kind": "parallel_delegation",
          "title": "并行搜索调研",
          "objective": "调研 Agent 上下文和记忆管理",
          "auto_start": True,
          "members": [
            {"participant_id": "researcher", "kind": "remote_agent", "role": "researcher", "connector_id": "codex"},
          ],
          "slices": [{"title": "论文搜索", "objective": "搜索论文"}],
        }
      )

      self.assertTrue(result.ok)
      self.assertTrue(result.output["workbench"]["auto_start_degraded"])
      self.assertIn("delegation broker", result.output["workbench"]["auto_start_degraded_reason"])
      self.assertEqual(result.output["workbench"]["workbench"]["kind"], "parallel_delegation")
      self.assertEqual(result.output["workbench"]["workbench"]["delegation_task_ids"], [])
    finally:
      conn.close()

  async def test_parallel_delegate_degrades_to_workbench_when_broker_missing(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      workbench_service = CollaborationWorkbenchService(uow_factory)
      provider = OrchestrationCapabilityProvider(
        uow_factory=uow_factory,
        task_launcher=_FakeTaskLauncher(),
        workbench_control=workbench_service,
      )

      result = await provider.parallel_delegate(
        {
          "parent_run_id": "run_parallel_missing_broker",
          "title": "4 个 Agent 并行搜索",
          "objective": "搜索深圳小学英语老师相关公开信息",
          "tasks": [
            {"connector_id": "codex-a", "agent_type": "codex", "task": "搜索公开网页"},
            {"connector_id": "codex-b", "agent_type": "codex", "task": "搜索学校公告"},
            {"connector_id": "codex-c", "agent_type": "codex", "task": "搜索新闻和社交平台"},
            {"connector_id": "codex-d", "agent_type": "codex", "task": "交叉验证来源"},
          ],
        }
      )

      self.assertTrue(result.ok)
      self.assertTrue(result.output["workbench"]["delegation_degraded"])
      self.assertFalse(result.output["delegation_status"]["started"])
      self.assertEqual(result.output["workbench"]["workbench"]["kind"], "parallel_delegation")
      self.assertEqual(len(result.output["workbench"]["task_slices"]), 4)
      self.assertEqual(result.output["workbench"]["workbench"]["delegation_task_ids"], [])
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


class _FakeTaskLauncher:
  async def create_task(self, payload):
    return {"task": {"run_id": payload.get("run_id"), "title": payload.get("title")}}


if __name__ == "__main__":
  unittest.main()
