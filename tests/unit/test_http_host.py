import io
import json
import unittest
from datetime import timedelta

from agent_kernel.agents import AgentDelegationBroker, ConnectorTurn, FakeAgentConnector
from agent_kernel.app.conversation_task_hub import ConversationTaskHub
from agent_kernel.app.config_center import ConfigCenterService
from agent_kernel.app.collaboration_workbench import CollaborationWorkbenchService
from agent_kernel.app.control_plane import ControlPlaneService
from agent_kernel.app.desktop_chat import DesktopChatService
from agent_kernel.app.orchestration_tools import CompositeToolCatalog, OrchestrationCapabilityIds, OrchestrationCapabilityProvider
from agent_kernel.autonomy import SkillService
from agent_kernel.capabilities import AtomicCapabilityIds, AtomicCapabilityProvider, CapabilityRegistry, CapabilityRuntime
from agent_kernel.capabilities.adapters import LocalToolExecutor
from agent_kernel.capabilities.adapters.control import ControlResult, ControlTarget, ControlWorkbench, FakeControlBackend
from agent_kernel.domain import (
  ArtifactRef,
  CapabilityGrant,
  NodeStepRecord,
  NodeStepStatus,
  RunState,
  RunStatus,
  RuntimeEvent,
  RuntimeEventType,
  ToolCallRecord,
  ToolCallStatus,
)
from agent_kernel.domain.base import utc_now
from agent_kernel.hosts.http import HTTPHost, SampleWorkflowTaskLauncher, make_handler
from agent_kernel.config import LLMConfig
from agent_kernel.models import MockModelProvider, ModelGateway
from agent_kernel.persistence import UnitOfWork, connect_sqlite
from agent_kernel.policy import ApprovalService
from agent_kernel.policy.engine import PolicyEngine
from agent_kernel.runtime import RuntimeEngine, unit_of_work_factory
from agent_kernel.workflow import NodeExecutorRegistry
from tests.integration.test_runtime_engine import build_three_node_workflow


class HTTPHostTests(unittest.TestCase):
  def test_http_host_exposes_run_events_and_artifact(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      workflow = build_three_node_workflow()
      engine = RuntimeEngine(uow_factory, NodeExecutorRegistry())
      run = engine.create_run(workflow, input={"request": "inspect"}, run_id="run_http")
      artifact = ArtifactRef("artifact_http", "artifact://http", media_type="text/plain")
      with UnitOfWork(conn) as uow:
        uow.artifacts.save(artifact, metadata={"purpose": "test"})
        uow.events.append(
          RuntimeEvent(
            event_type=RuntimeEventType.ARTIFACT_CREATED,
            run_id=run.run_id,
            artifact_refs=[artifact],
            payload={"artifact_id": artifact.artifact_id},
          )
        )
      handler = make_handler(HTTPHost(uow_factory))

      run_payload = self._request_json(handler, "GET", "/runs/run_http")
      event_lines = self._request_ndjson(handler, "/runs/run_http/events")
      artifact_payload = self._request_json(handler, "GET", "/artifacts/artifact_http")

      self.assertTrue(run_payload["ok"])
      self.assertEqual(run_payload["run"]["run_id"], "run_http")
      self.assertGreaterEqual(len(event_lines), 2)
      self.assertEqual(event_lines[-1]["event_type"], "artifact.created")
      self.assertEqual(artifact_payload["artifact"]["artifact_id"], "artifact_http")
      self.assertEqual(artifact_payload["metadata"], {"purpose": "test"})
    finally:
      conn.close()

  def test_http_host_streams_run_events_as_sse(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      engine = RuntimeEngine(uow_factory, NodeExecutorRegistry())
      engine.create_run(build_three_node_workflow(), input={}, run_id="run_sse")
      handler = make_handler(HTTPHost(uow_factory))

      status, headers, body = _dispatch_fake_request_with_headers(
        handler,
        "GET",
        "/runs/run_sse/events?format=sse",
        headers={"Accept": "text/event-stream"},
      )
      text = body.decode("utf-8")

      self.assertIn("200 OK", status)
      self.assertEqual(headers["Content-Type"], "text/event-stream; charset=utf-8")
      self.assertIn("event: run.created", text)
      self.assertIn("data: ", text)
      self.assertTrue(text.endswith("\n\n"))
    finally:
      conn.close()

  def test_http_host_exposes_runtime_write_controls(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      approval_service = ApprovalService(uow_factory)
      approve_request = approval_service.request_tool_approval("run_http_write", "tool.approve", "approval", {})
      reject_request = approval_service.request_tool_approval("run_http_write", "tool.reject", "approval", {})
      with UnitOfWork(conn) as uow:
        uow.states.save(RunState(run_id="run_http_write", status=RunStatus.RUNNING))
        uow.tool_calls.save(
          ToolCallRecord(
            tool_call_id="tool_call_http_cancel",
            run_id="run_http_write",
            capability_id="proc.long",
            status=ToolCallStatus.RUNNING,
          )
        )
        uow.tool_calls.save(
          ToolCallRecord(
            tool_call_id="tool_call_http_kill",
            run_id="run_http_write",
            capability_id="proc.long",
            status=ToolCallStatus.RUNNING,
          )
        )
      handler = make_handler(HTTPHost(uow_factory))

      intervention = self._request_json(
        handler,
        "POST",
        "/runs/run_http_write/interventions",
        {
          "content": "prefer interface-first controls",
          "mode": "pause_and_resume",
          "priority": "critical",
        },
      )
      cancel_tool = self._request_json(
        handler,
        "POST",
        "/tool-calls/tool_call_http_cancel/cancel",
        {"grace_seconds": 0.5},
      )
      kill_tool = self._request_json(handler, "POST", "/tool-calls/tool_call_http_kill/kill")
      approve = self._request_json(
        handler,
        "POST",
        f"/approvals/{approve_request.approval_id}/approve",
        {"ttl_seconds": 60},
      )
      reject = self._request_json(handler, "POST", f"/approvals/{reject_request.approval_id}/reject")
      cancel_run = self._request_json(
        handler,
        "POST",
        "/runs/run_http_write/cancel",
        {"reason": "covered by HTTP control"},
      )

      self.assertTrue(intervention["ok"])
      self.assertEqual(intervention["run_status"], "interrupted")
      self.assertIsNotNone(intervention["memory_id"])
      self.assertTrue(cancel_tool["ok"])
      self.assertEqual(cancel_tool["requested_status"], "cancelling")
      self.assertEqual(cancel_tool["tool_call"]["status"], "cancelling")
      self.assertTrue(kill_tool["ok"])
      self.assertEqual(kill_tool["requested_status"], "killing")
      self.assertEqual(kill_tool["tool_call"]["status"], "killing")
      self.assertTrue(approve["ok"])
      self.assertEqual(approve["grant"]["capability_id"], "tool.approve")
      self.assertTrue(reject["ok"])
      self.assertEqual(reject["approval"]["status"], "rejected")
      self.assertTrue(cancel_run["ok"])
      self.assertEqual(cancel_run["run"]["status"], "cancelled")

      with UnitOfWork(conn) as uow:
        events = uow.events.list_by_run("run_http_write")
        state = uow.states.get("run_http_write")
      event_types = [event.event_type for event in events]
      self.assertEqual(state.status, RunStatus.CANCELLED)
      self.assertIn(RuntimeEventType.HUMAN_INTERVENTION, event_types)
      self.assertIn(RuntimeEventType.TOOL_CALL_CANCEL_REQUESTED, event_types)
      self.assertIn(RuntimeEventType.TOOL_CALL_KILL_REQUESTED, event_types)
      self.assertIn(RuntimeEventType.RUN_CANCELLED, event_types)
    finally:
      conn.close()

  def test_http_host_can_create_task_backed_by_runtime_run(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      handler = make_handler(HTTPHost(uow_factory))

      status, _headers, body = _dispatch_fake_request_with_headers(
        handler,
        "POST",
        "/tasks",
        {
          "title": "replace generic agent flow",
          "run_id": "run_http_task",
          "input": {"text": "hello"},
        },
      )
      payload = json.loads(body.decode("utf-8"))

      self.assertIn("201 Created", status)
      self.assertTrue(payload["ok"])
      self.assertEqual(payload["task"]["run_id"], "run_http_task")
      self.assertEqual(payload["task"]["status"], "completed")
      self.assertEqual(payload["run"]["variables"]["echo"], "replace generic agent flow")
      with UnitOfWork(conn) as uow:
        state = uow.states.get("run_http_task")
        events = uow.events.list_by_run("run_http_task")
      self.assertEqual(state.status, RunStatus.COMPLETED)
      self.assertIn(RuntimeEventType.RUN_CREATED, [event.event_type for event in events])
      self.assertIn(RuntimeEventType.RUN_COMPLETED, [event.event_type for event in events])
    finally:
      conn.close()

  def test_http_host_can_create_task_without_starting_run(self) -> None:
    conn = connect_sqlite()
    try:
      handler = make_handler(HTTPHost(unit_of_work_factory(conn)))

      payload = self._request_json(
        handler,
        "POST",
        "/tasks",
        {
          "title": "draft task",
          "run_id": "run_http_task_pending",
          "start": False,
        },
      )

      self.assertTrue(payload["ok"])
      self.assertEqual(payload["task"]["status"], "pending")
      self.assertEqual(payload["run"]["current_node_id"], "echo")
    finally:
      conn.close()

  def test_http_host_exposes_agent_delegation_controls(self) -> None:
    conn = connect_sqlite()
    try:
      connector = FakeAgentConnector()
      connector.queue_response(
        ConnectorTurn(
          turn_id="turn_http_delegate",
          session_id="unused",
          output={"summary": "done"},
          completed=True,
        )
      )
      broker = AgentDelegationBroker(unit_of_work_factory(conn), {"codex": connector})
      handler = make_handler(HTTPHost(unit_of_work_factory(conn), delegation_control=broker))

      status, _headers, body = _dispatch_fake_request_with_headers(
        handler,
        "POST",
        "/delegations",
        {
          "parent_run_id": "run_http_delegate",
          "connector_id": "codex",
          "agent_type": "implementation",
          "task": "implement HTTP delegation",
        },
      )
      created = json.loads(body.decode("utf-8"))
      task_id = created["delegation"]["task_id"]
      status_payload = self._request_json(
        handler,
        "POST",
        "/delegations/status",
        {
          "parent_run_id": "run_http_delegate",
          "task_ids": [task_id],
          "wait_ms": 500,
        },
      )
      get_payload = self._request_json(
        handler,
        "GET",
        f"/delegations/{task_id}?parent_run_id=run_http_delegate",
      )

      self.assertIn("201 Created", status)
      self.assertTrue(created["ok"])
      self.assertEqual(status_payload["delegations"][0]["status"], "completed")
      self.assertEqual(get_payload["delegations"][0]["output"], {"summary": "done"})
      self.assertEqual(connector.messages[0].content["task"], "implement HTTP delegation")
    finally:
      conn.close()

  def test_http_delegation_endpoint_requires_configured_broker(self) -> None:
    conn = connect_sqlite()
    try:
      handler = make_handler(HTTPHost(unit_of_work_factory(conn)))

      payload = self._request_json(
        handler,
        "POST",
        "/delegations/status",
        {"parent_run_id": "run_missing", "task_ids": ["delegation_missing"]},
      )

      self.assertFalse(payload["ok"])
      self.assertIn("not configured", payload["error"])
    finally:
      conn.close()

  def test_http_intervention_can_cancel_current_step_and_resume(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      with UnitOfWork(conn) as uow:
        uow.states.save(RunState(run_id="run_http_step", status=RunStatus.RUNNING, current_node_id="work"))
        uow.steps.save(
          NodeStepRecord(
            step_id="step_http_running",
            run_id="run_http_step",
            node_id="work",
            status=NodeStepStatus.RUNNING,
          )
        )
      handler = make_handler(HTTPHost(uow_factory))

      payload = self._request_json(
        handler,
        "POST",
        "/runs/run_http_step/interventions",
        {
          "content": "interrupt current step",
          "mode": "cancel_current_step_and_resume",
        },
      )

      with UnitOfWork(conn) as uow:
        step = uow.steps.get("step_http_running")
      self.assertTrue(payload["ok"])
      self.assertEqual(payload["run_status"], "running")
      self.assertEqual(payload["interrupted_step_id"], "step_http_running")
      self.assertEqual(step.status, NodeStepStatus.INTERRUPTED)
    finally:
      conn.close()

  def test_http_host_rejects_invalid_json_body(self) -> None:
    conn = connect_sqlite()
    try:
      handler = make_handler(HTTPHost(unit_of_work_factory(conn)))
      response = _dispatch_fake_request(
        handler,
        "POST",
        "/runs/run_1/interventions",
        b"{not-json",
      )
      payload = json.loads(response.decode("utf-8"))
      self.assertFalse(payload["ok"])
      self.assertIn("error", payload)
    finally:
      conn.close()

  def test_http_host_exposes_mcp_scheduled_task_and_control_interfaces(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      handler = make_handler(
        HTTPHost(
          uow_factory,
          control_plane=ControlPlaneService.from_config({"fake": True, "browser": {"enabled": False}}),
        )
      )
      due_at = (utc_now() - timedelta(minutes=1)).isoformat()

      mcp_created = self._request_json(
        handler,
        "POST",
        "/mcp-servers",
        {
          "name": "echo",
          "enabled": True,
          "transport": {"type": "stdio", "command": "echo", "args": ["mcp"]},
        },
      )
      mcp_listed = self._request_json(handler, "GET", "/mcp-servers?enabled_only=true")
      scheduled = self._request_json(
        handler,
        "POST",
        "/scheduled-tasks",
        {
          "task_id": "scheduled_http",
          "name": "HTTP schedule",
          "schedule_kind": "at",
          "schedule_value": due_at,
          "payload": {"title": "scheduled http", "run_id": "run_scheduled_http"},
        },
      )
      triggered = self._request_json(handler, "POST", "/scheduled-tasks/run-due", {})
      control_health = self._request_json(handler, "GET", "/control/health")
      control_targets = self._request_json(handler, "GET", "/control/targets?kind=browser")
      deleted = self._request_json(handler, "DELETE", "/mcp-servers/echo")

      self.assertTrue(mcp_created["ok"])
      self.assertEqual(mcp_listed["mcp_servers"][0]["name"], "echo")
      self.assertEqual(scheduled["scheduled_task"]["task_id"], "scheduled_http")
      self.assertEqual(triggered["triggers"][0]["result"]["task"]["run_id"], "run_scheduled_http")
      self.assertTrue(control_health["ok"])
      self.assertEqual(control_targets["targets"], [])
      self.assertTrue(deleted["deleted"])
    finally:
      conn.close()

  def test_http_host_exposes_desktop_workspace_api_surface(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      approval_service = ApprovalService(uow_factory)
      approval = approval_service.request_tool_approval("run_desktop", "tool.write", "approval", {"path": "x"})
      artifact = ArtifactRef("artifact_desktop", "artifact://desktop", media_type="text/plain")
      with UnitOfWork(conn) as uow:
        uow.states.save(
          RunState(
            run_id="run_desktop",
            status=RunStatus.RUNNING,
            task_id="task_desktop",
            artifact_refs=[artifact],
          )
        )
        uow.artifacts.save(artifact, metadata={"kind": "desktop"})
        uow.tool_calls.save(
          ToolCallRecord(
            tool_call_id="tool_call_desktop",
            run_id="run_desktop",
            capability_id="tool.write",
            status=ToolCallStatus.RUNNING,
          )
        )
      handler = make_handler(HTTPHost(uow_factory))

      workspaces = self._request_json(handler, "GET", "/workspaces")
      workspace_id = workspaces["workspaces"][0]["workspace_id"]
      workspace = self._request_json(handler, "GET", f"/workspaces/{workspace_id}")
      approvals = self._request_json(handler, "GET", "/approvals")
      tool_calls = self._request_json(handler, "GET", "/tool-calls?run_id=run_desktop")

      self.assertEqual(workspace_id, "workspace_task_task_desktop")
      self.assertEqual(workspace["workspace"]["run_ids"], ["run_desktop"])
      self.assertEqual(workspace["workspace"]["artifact_ids"], ["artifact_desktop"])
      self.assertEqual(workspace["workspace"]["pending_approval_ids"], [approval.approval_id])
      self.assertEqual(workspace["workspace"]["active_tool_call_ids"], ["tool_call_desktop"])
      self.assertEqual(approvals["approvals"][0]["approval_id"], approval.approval_id)
      self.assertEqual(tool_calls["tool_calls"][0]["tool_call_id"], "tool_call_desktop")
    finally:
      conn.close()

  def test_http_host_supports_event_cursor_scheduled_mutation_and_control_command(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      backend = FakeControlBackend()
      backend.register_target(ControlTarget(target_id="browser_1", kind="browser", label="Browser"))
      backend.register_response("browser", "inspect", ControlResult(ok=True, output={"title": "Browser"}))
      control_plane = ControlPlaneService(ControlWorkbench(backend))
      handler = make_handler(HTTPHost(uow_factory, control_plane=control_plane))
      due_at = (utc_now() - timedelta(minutes=1)).isoformat()
      with UnitOfWork(conn) as uow:
        uow.events.append(RuntimeEvent(event_type=RuntimeEventType.RUN_CREATED, run_id="run_cursor", payload={}))
        second = RuntimeEvent(event_type=RuntimeEventType.RUN_COMPLETED, run_id="run_cursor", payload={})
        uow.events.append(second)

      events = self._request_ndjson(handler, f"/events?run_id=run_cursor&after_event_id={second.event_id}")
      scheduled = self._request_json(
        handler,
        "POST",
        "/scheduled-tasks",
        {
          "task_id": "scheduled_mutation",
          "name": "mutable",
          "schedule_kind": "at",
          "schedule_value": due_at,
          "payload": {"title": "mutable", "run_id": "run_mutable"},
        },
      )
      updated = self._request_json(
        handler,
        "PATCH",
        "/scheduled-tasks/scheduled_mutation",
        {"enabled": False, "name": "disabled mutable"},
      )
      triggered = self._request_json(handler, "POST", "/scheduled-tasks/run-due", {})
      triggers = self._request_json(handler, "GET", "/scheduled-tasks/scheduled_mutation/triggers")
      command = self._request_json(
        handler,
        "POST",
        "/control/commands",
        {"target_kind": "browser", "action": "inspect", "target_id": "browser_1"},
      )
      deleted = self._request_json(handler, "DELETE", "/scheduled-tasks/scheduled_mutation")

      self.assertEqual(events, [])
      self.assertEqual(scheduled["scheduled_task"]["task_id"], "scheduled_mutation")
      self.assertFalse(updated["scheduled_task"]["enabled"])
      self.assertEqual(updated["scheduled_task"]["name"], "disabled mutable")
      self.assertEqual(triggered["triggers"], [])
      self.assertEqual(triggers["triggers"], [])
      self.assertTrue(command["result"]["ok"])
      self.assertEqual(command["command"]["target_id"], "browser_1")
      self.assertTrue(deleted["deleted"])
    finally:
      conn.close()

  def test_http_host_exposes_chat_skill_and_config_center(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      gateway = ModelGateway()
      gateway.register_provider(
        "mock",
        MockModelProvider(
          responses=[
            {"content": "这是模型回复"},
            {"content": "这是重试后的模型回复"},
          ]
        ),
      )
      chat_service = DesktopChatService(
        uow_factory,
        SampleWorkflowTaskLauncher(uow_factory),
        model_gateway=gateway,
        model_provider_name="mock",
        model_ref="mock-model",
      )
      handler = make_handler(HTTPHost(uow_factory, desktop_chat_service=chat_service))

      created_session = self._request_json(
        handler,
        "POST",
        "/chat/sessions",
        {"title": "默认对话"},
      )
      session_id = created_session["chat_session"]["session_id"]
      sent = self._request_json(
        handler,
        "POST",
        f"/chat/sessions/{session_id}/messages",
        {"content": "你好，帮我看一下今天的任务"},
      )
      messages = self._request_json(handler, "GET", f"/chat/sessions/{session_id}/messages")
      sessions = self._request_json(handler, "GET", "/chat/sessions")
      retried = self._request_json(handler, "POST", f"/chat/sessions/{session_id}/retry")
      paused = self._request_json(handler, "POST", f"/chat/sessions/{session_id}/pause", {"reason": "test pause"})
      cleared = self._request_json(handler, "DELETE", f"/chat/sessions/{session_id}/messages")
      empty_messages = self._request_json(handler, "GET", f"/chat/sessions/{session_id}/messages")

      created_skill = self._request_json(
        handler,
        "POST",
        "/skills",
        {
          "name": "代码评审",
          "description": "评审代码风险",
          "when_to_use": "需要 review 时",
          "instructions": "列出风险和测试缺口",
          "status": "active",
        },
      )
      skill_id = created_skill["skill"]["skill_id"]
      updated_skill = self._request_json(
        handler,
        "PATCH",
        f"/skills/{skill_id}",
        {"description": "评审代码风险和回归风险"},
      )
      skills = self._request_json(handler, "GET", "/skills?active_only=true")
      deprecated = self._request_json(handler, "POST", f"/skills/{skill_id}/deprecate")

      config_sections = self._request_json(handler, "GET", "/config")
      updated_config = self._request_json(
        handler,
        "PATCH",
        "/config/llm",
        {
          "data": {
            "model": "gpt-test",
            "api_key": "secret-value",
          }
        },
      )
      llm_config = self._request_json(handler, "GET", "/config/llm")

      self.assertEqual(created_session["chat_session"]["title"], "默认对话")
      self.assertEqual(sent["messages"][0]["role"], "user")
      self.assertEqual(sent["messages"][1]["role"], "assistant")
      self.assertEqual(sent["messages"][1]["content"], "这是模型回复")
      self.assertEqual(len(messages["messages"]), 2)
      self.assertEqual(sessions["chat_sessions"][0]["message_count"], 2)
      self.assertEqual(retried["messages"][0]["role"], "user")
      self.assertEqual(retried["messages"][1]["content"], "这是重试后的模型回复")
      self.assertEqual(paused["session"]["status"], "paused")
      self.assertEqual(cleared["session"]["message_count"], 0)
      self.assertEqual(empty_messages["messages"], [])
      self.assertEqual(created_skill["skill"]["status"], "active")
      self.assertEqual(updated_skill["skill"]["description"], "评审代码风险和回归风险")
      self.assertIn(skill_id, [skill["skill_id"] for skill in skills["skills"]])
      self.assertEqual(deprecated["skill"]["status"], "deprecated")
      self.assertGreaterEqual(len(config_sections["config_sections"]), 1)
      self.assertEqual(updated_config["config_section"]["data"]["model"], "gpt-test")
      self.assertEqual(updated_config["config_section"]["data"]["api_key"], "***")
      self.assertEqual(llm_config["config_section"]["data"]["api_key"], "***")
    finally:
      conn.close()

  def test_default_chat_entry_records_conversation_task_links(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      handler = make_handler(HTTPHost(uow_factory))

      created_session = self._request_json(handler, "POST", "/chat/sessions", {"title": "日常对话"})
      session_id = created_session["chat_session"]["session_id"]
      sent = self._request_json(
        handler,
        "POST",
        f"/chat/sessions/{session_id}/messages",
        {"content": "帮我搜索今天的天气", "metadata": {"workspace_id": "workspace_default"}},
      )

      assistant = sent["messages"][1]
      conversation_task = assistant["metadata"]["task_result"]["conversation_task"]
      with UnitOfWork(conn) as uow:
        thread_record_id = f"conversation_thread:{session_id}"
        thread = uow.interactions.get_record("conversation_thread", thread_record_id)
        message = uow.interactions.get_record(
          "conversation_message",
          f"conversation_message:{sent['messages'][0]['message_id']}",
        )
        task = uow.interactions.get_record("task", f"task:{conversation_task['task']['task_id']}")
        links = uow.interactions.list_records("thread_task_link", thread_record_id)

      self.assertEqual(conversation_task["thread"]["thread_id"], session_id)
      self.assertEqual(conversation_task["message"]["content"]["text"], "帮我搜索今天的天气")
      self.assertEqual(conversation_task["task"]["status"], "ready")
      self.assertEqual(conversation_task["run_id"], assistant["run_id"])
      self.assertEqual(thread["workspace_id"], "workspace_default")
      self.assertEqual(message["role"], "user")
      self.assertEqual(task["title"], "帮我搜索今天的天气")
      self.assertEqual(links[0]["run_id"], assistant["run_id"])
    finally:
      conn.close()

  def test_default_chat_daily_agent_executes_model_selected_tool(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      gateway = ModelGateway()
      provider = MockModelProvider(
        responses=[
          {
            "tool_calls": [
              {
                "name": "task_status",
                "input": {"run_id": "chat_tool_run"},
              }
            ]
          },
          {
            "finish": True,
            "output": {"content": "已经查询到任务运行状态。"},
          },
        ]
      )
      gateway.register_provider("mock", provider)
      launcher = SampleWorkflowTaskLauncher(uow_factory)
      registry = CapabilityRegistry()
      local_tools = LocalToolExecutor()
      orchestration = OrchestrationCapabilityProvider(
        uow_factory=uow_factory,
        task_launcher=launcher,
        run_control=RuntimeEngine(uow_factory, NodeExecutorRegistry()),
      )
      orchestration.register(registry, local_tools)
      runtime = CapabilityRuntime(
        registry,
        PolicyEngine(
          grants=[
            CapabilityGrant(
              grant_id="grant_task_status",
              capability_id=OrchestrationCapabilityIds.TASK_STATUS,
              expires_at=utc_now() + timedelta(minutes=5),
            )
          ]
        ),
        local_tools,
        uow_factory=uow_factory,
      )
      chat_service = DesktopChatService(
        uow_factory,
        launcher,
        model_gateway=gateway,
        model_provider_name="mock",
        model_ref="mock-model",
        skill_service=SkillService(uow_factory),
        capability_runtime=runtime,
        atomic_capabilities=CompositeToolCatalog([orchestration]),
        conversation_task_hub=ConversationTaskHub(uow_factory, launcher),
      )
      handler = make_handler(HTTPHost(uow_factory, desktop_chat_service=chat_service))

      created_session = self._request_json(handler, "POST", "/chat/sessions", {"title": "日常对话"})
      session_id = created_session["chat_session"]["session_id"]
      sent = self._request_json(
        handler,
        "POST",
        f"/chat/sessions/{session_id}/messages",
        {"content": "查一下当前任务状态", "run_id": "chat_tool_run"},
      )

      self.assertEqual(sent["messages"][1]["content"], "已经查询到任务运行状态。")
      daily_agent = sent["messages"][1]["metadata"]["llm_result"]["daily_agent"]
      self.assertEqual(daily_agent["tool_calls"][0]["name"], "task_status")
      self.assertEqual(daily_agent["tool_calls"][0]["capability_id"], "meadow.task.status")
      self.assertTrue(daily_agent["tool_calls"][0]["ok"])
      tool_names = [schema["function"]["name"] for schema in provider.calls[0][1].tool_schemas]
      self.assertIn("task_status", tool_names)
      with UnitOfWork(conn) as uow:
        tool_calls = uow.tool_calls.list_by_run("chat_tool_run")
      self.assertEqual(tool_calls[0].capability_id, "meadow.task.status")
    finally:
      conn.close()

  def test_default_chat_daily_agent_can_create_and_advance_workbench_by_tool_choice(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      gateway = ModelGateway()
      provider = MockModelProvider(
        responses=[
          {
            "tool_calls": [
              {
                "name": "workbench_create",
                "input": {
                  "kind": "group_chat",
                  "title": "评审群聊",
                  "objective": "组织多个角色评审当前方案",
                  "members": [
                    {"participant_id": "daily_agent", "kind": "agent", "role": "moderator"},
                    {"participant_id": "human_user", "kind": "human", "role": "owner"},
                    {"participant_id": "reviewer", "kind": "agent", "role": "reviewer"},
                  ],
                },
              }
            ]
          },
          {
            "tool_calls": [
              {
                "name": "workbench_message",
                "input": {
                  "workbench_id": "FROM_TOOL_RESULT",
                  "sender_participant_id": "daily_agent",
                  "text": "请 reviewer 从架构风险角度先给出意见。",
                },
              }
            ]
          },
          {
            "finish": True,
            "output": {"content": "已创建评审群聊，并以主持人身份发起第一轮讨论。"},
          },
        ]
      )

      class WorkbenchAwareGateway(ModelGateway):
        def __init__(self):
          super().__init__()
          self._calls = 0

        async def complete(self, provider_name, model_ref, context):
          self._calls += 1
          if self._calls == 2:
            tool_result = context.messages[-1]["content"]["results"][0]["output"]
            workbench_id = tool_result["workbench"]["workbench"]["workbench_id"]
            provider.responses[0]["tool_calls"][0]["input"]["workbench_id"] = workbench_id
          return await super().complete(provider_name, model_ref, context)

      gateway = WorkbenchAwareGateway()
      gateway.register_provider("mock", provider)
      launcher = SampleWorkflowTaskLauncher(uow_factory)
      registry = CapabilityRegistry()
      local_tools = LocalToolExecutor()
      workbench_service = CollaborationWorkbenchService(uow_factory)
      orchestration = OrchestrationCapabilityProvider(
        uow_factory=uow_factory,
        task_launcher=launcher,
        run_control=RuntimeEngine(uow_factory, NodeExecutorRegistry()),
        workbench_control=workbench_service,
      )
      orchestration.register(registry, local_tools)
      runtime = CapabilityRuntime(
        registry,
        PolicyEngine(
          grants=[
            CapabilityGrant(
              grant_id=f"grant_{capability_id}",
              capability_id=capability_id,
              expires_at=utc_now() + timedelta(minutes=5),
            )
            for capability_id in [
              OrchestrationCapabilityIds.WORKBENCH_CREATE,
              OrchestrationCapabilityIds.WORKBENCH_MESSAGE,
            ]
          ]
        ),
        local_tools,
        uow_factory=uow_factory,
      )
      chat_service = DesktopChatService(
        uow_factory,
        launcher,
        model_gateway=gateway,
        model_provider_name="mock",
        model_ref="mock-model",
        skill_service=SkillService(uow_factory),
        capability_runtime=runtime,
        atomic_capabilities=CompositeToolCatalog([orchestration]),
        conversation_task_hub=ConversationTaskHub(uow_factory, launcher),
      )
      handler = make_handler(HTTPHost(uow_factory, desktop_chat_service=chat_service))

      created_session = self._request_json(handler, "POST", "/chat/sessions", {"title": "日常对话"})
      session_id = created_session["chat_session"]["session_id"]
      sent = self._request_json(
        handler,
        "POST",
        f"/chat/sessions/{session_id}/messages",
        {"content": "组织一个技术评审群聊并先推进一轮", "run_id": "chat_workbench_run"},
      )

      self.assertEqual(sent["messages"][1]["content"], "已创建评审群聊，并以主持人身份发起第一轮讨论。")
      daily_agent = sent["messages"][1]["metadata"]["llm_result"]["daily_agent"]
      self.assertEqual([call["name"] for call in daily_agent["tool_calls"]], ["workbench_create", "workbench_message"])
      self.assertTrue(all(call["ok"] for call in daily_agent["tool_calls"]))
      workbench_id = daily_agent["tool_calls"][0]["output"]["workbench"]["workbench"]["workbench_id"]
      snapshot = workbench_service.snapshot(workbench_id)
      self.assertEqual(snapshot.workbench.kind, "group_chat")
      self.assertEqual(snapshot.messages[0].sender_participant_id, "daily_agent")
      self.assertIn("workbench_create", [schema["function"]["name"] for schema in provider.calls[0][1].tool_schemas])
      with UnitOfWork(conn) as uow:
        calls = uow.tool_calls.list_by_run("chat_workbench_run")
      self.assertEqual([call.capability_id for call in calls], [
        OrchestrationCapabilityIds.WORKBENCH_CREATE,
        OrchestrationCapabilityIds.WORKBENCH_MESSAGE,
      ])
    finally:
      conn.close()

  def test_default_chat_daily_agent_summarizes_workbench_when_model_finishes_empty(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      gateway = ModelGateway()
      provider = MockModelProvider(
        responses=[
          {
            "tool_calls": [
              {
                "name": "workbench_create",
                "input": {
                  "kind": "parallel_delegation",
                  "title": "并行搜索调研",
                  "objective": "起多个 Agent 进行搜索调研",
                  "members": [
                    {"participant_id": "daily_agent", "kind": "agent", "role": "moderator"},
                    {"participant_id": "researcher_a", "kind": "agent", "role": "searcher"},
                    {"participant_id": "researcher_b", "kind": "agent", "role": "searcher"},
                  ],
                  "slices": [
                    {"title": "搜索方向 A", "objective": "搜索资料 A"},
                    {"title": "搜索方向 B", "objective": "搜索资料 B"},
                  ],
                },
              }
            ]
          },
          {"content": ""},
        ]
      )
      gateway.register_provider("mock", provider)
      launcher = SampleWorkflowTaskLauncher(uow_factory)
      registry = CapabilityRegistry()
      local_tools = LocalToolExecutor()
      workbench_service = CollaborationWorkbenchService(uow_factory)
      orchestration = OrchestrationCapabilityProvider(
        uow_factory=uow_factory,
        task_launcher=launcher,
        run_control=RuntimeEngine(uow_factory, NodeExecutorRegistry()),
        workbench_control=workbench_service,
      )
      orchestration.register(registry, local_tools)
      runtime = CapabilityRuntime(
        registry,
        PolicyEngine(
          grants=[
            CapabilityGrant(
              grant_id="grant_workbench_empty_finish",
              capability_id=OrchestrationCapabilityIds.WORKBENCH_CREATE,
              expires_at=utc_now() + timedelta(minutes=5),
            )
          ]
        ),
        local_tools,
        uow_factory=uow_factory,
      )
      chat_service = DesktopChatService(
        uow_factory,
        launcher,
        model_gateway=gateway,
        model_provider_name="mock",
        model_ref="mock-model",
        skill_service=SkillService(uow_factory),
        capability_runtime=runtime,
        atomic_capabilities=CompositeToolCatalog([orchestration]),
        conversation_task_hub=ConversationTaskHub(uow_factory, launcher),
      )
      handler = make_handler(HTTPHost(uow_factory, desktop_chat_service=chat_service))

      created_session = self._request_json(handler, "POST", "/chat/sessions", {"title": "日常对话"})
      session_id = created_session["chat_session"]["session_id"]
      sent = self._request_json(
        handler,
        "POST",
        f"/chat/sessions/{session_id}/messages",
        {"content": "我想执行一个搜索任务，你可以起多个 agent 帮我进行搜索调研吗？", "run_id": "chat_empty_workbench_run"},
      )

      self.assertIn("已创建/推进协作工作台", sent["messages"][1]["content"])
      self.assertIn("并行搜索调研", sent["messages"][1]["content"])
      daily_agent = sent["messages"][1]["metadata"]["llm_result"]["daily_agent"]
      self.assertEqual(daily_agent["tool_calls"][0]["name"], "workbench_create")
      self.assertTrue(daily_agent["tool_calls"][0]["ok"])
    finally:
      conn.close()

  def test_default_chat_daily_agent_persists_waiting_for_user_state(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      gateway = ModelGateway()
      provider = MockModelProvider(
        responses=[
          {
            "tool_calls": [
              {
                "name": "user_input_request",
                "input": {
                  "question": "请提供要参与工作台的 CLI connector_id。",
                  "candidates": ["codex_cli", "claude_cli"],
                },
              }
            ]
          }
        ]
      )
      gateway.register_provider("mock", provider)
      launcher = SampleWorkflowTaskLauncher(uow_factory)
      registry = CapabilityRegistry()
      local_tools = LocalToolExecutor()
      atomic = AtomicCapabilityProvider()
      atomic.register(registry, local_tools)
      runtime = CapabilityRuntime(
        registry,
        PolicyEngine(
          grants=[
            CapabilityGrant(
              grant_id="grant_user_input",
              capability_id=AtomicCapabilityIds.USER_INPUT_REQUEST,
              expires_at=utc_now() + timedelta(minutes=5),
            )
          ]
        ),
        local_tools,
        uow_factory=uow_factory,
      )
      chat_service = DesktopChatService(
        uow_factory,
        launcher,
        model_gateway=gateway,
        model_provider_name="mock",
        model_ref="mock-model",
        skill_service=SkillService(uow_factory),
        capability_runtime=runtime,
        atomic_capabilities=CompositeToolCatalog([atomic]),
        conversation_task_hub=ConversationTaskHub(uow_factory, launcher),
      )
      handler = make_handler(HTTPHost(uow_factory, desktop_chat_service=chat_service))

      created_session = self._request_json(handler, "POST", "/chat/sessions", {"title": "日常对话"})
      session_id = created_session["chat_session"]["session_id"]
      sent = self._request_json(
        handler,
        "POST",
        f"/chat/sessions/{session_id}/messages",
        {"content": "启动多 CLI 协同", "run_id": "chat_waiting_run"},
      )

      self.assertEqual(sent["session"]["status"], "waiting_for_user")
      self.assertIn("请提供要参与工作台的 CLI connector_id", sent["messages"][1]["content"])
      self.assertEqual(sent["session"]["metadata"]["active_run_id"], "chat_waiting_run")
      self.assertEqual(sent["messages"][1]["metadata"]["pending"]["question"], "请提供要参与工作台的 CLI connector_id。")
    finally:
      conn.close()

  def test_default_chat_daily_agent_receives_session_history(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      gateway = ModelGateway()
      provider = MockModelProvider(
        responses=[
          {"finish": True, "output": {"content": "我需要先知道城市。"}},
          {"finish": True, "output": {"content": "深圳今天多云。"}},
        ]
      )
      gateway.register_provider("mock", provider)
      launcher = SampleWorkflowTaskLauncher(uow_factory)
      runtime = CapabilityRuntime(
        CapabilityRegistry(),
        PolicyEngine(grants=[]),
        LocalToolExecutor(),
        uow_factory=uow_factory,
      )
      chat_service = DesktopChatService(
        uow_factory,
        launcher,
        model_gateway=gateway,
        model_provider_name="mock",
        model_ref="mock-model",
        skill_service=SkillService(uow_factory),
        capability_runtime=runtime,
        atomic_capabilities=CompositeToolCatalog([]),
        conversation_task_hub=ConversationTaskHub(uow_factory, launcher),
      )
      handler = make_handler(HTTPHost(uow_factory, desktop_chat_service=chat_service))

      created_session = self._request_json(handler, "POST", "/chat/sessions", {"title": "日常对话"})
      session_id = created_session["chat_session"]["session_id"]
      self._request_json(
        handler,
        "POST",
        f"/chat/sessions/{session_id}/messages",
        {"content": "帮我用浏览器查看一下今天的天气"},
      )
      second = self._request_json(
        handler,
        "POST",
        f"/chat/sessions/{session_id}/messages",
        {"content": "深圳"},
      )

      self.assertEqual(second["messages"][1]["content"], "深圳今天多云。")
      second_context = provider.calls[1][1]
      history = [
        message
        for message in second_context.messages
        if message.get("role") in {"user", "assistant"} and isinstance(message.get("content"), str)
      ]
      self.assertEqual(history[-3]["content"], "帮我用浏览器查看一下今天的天气")
      self.assertEqual(history[-2]["content"], "我需要先知道城市。")
      self.assertEqual(history[-1]["content"], "深圳")
    finally:
      conn.close()

  def test_desktop_chat_uses_default_llm_config_when_config_center_is_empty(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      service = DesktopChatService(
        uow_factory,
        SampleWorkflowTaskLauncher(uow_factory),
        default_llm_config=LLMConfig(
          provider="openai-compatible",
          model="model-from-file",
          api_key="key-from-file",
          base_url="https://llm.example/v1",
          timeout_seconds=99,
        ),
      )

      config = service._load_llm_config()

      self.assertEqual(config.model, "model-from-file")
      self.assertEqual(config.api_key, "key-from-file")
      self.assertEqual(config.base_url, "https://llm.example/v1")
      self.assertEqual(config.timeout_seconds, 99)
    finally:
      conn.close()

  def test_desktop_chat_reads_provider_model_agent_binding_config(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      ConfigCenterService(uow_factory).update_section(
        "llm",
        {
          "active_provider_id": "gemini_primary",
          "active_model_id": "gemini-2.5-flash",
          "providers": [
            {
              "provider_id": "gemini_primary",
              "name": "Gemini",
              "kind": "gemini",
              "base_url": "https://gemini.example/v1beta",
              "api_key": "gemini-secret",
              "timeout_seconds": 22,
              "models": [
                {
                  "model_id": "gemini-2.5-flash",
                  "enabled": True,
                  "capabilities": {"text": True, "vision": True, "function_calling": True},
                }
              ],
            }
          ],
          "agent_bindings": [
            {
              "agent_id": "desktop_daily_agent",
              "provider_id": "gemini_primary",
              "model_id": "gemini-2.5-flash",
            }
          ],
        },
        merge=False,
      )
      service = DesktopChatService(uow_factory, SampleWorkflowTaskLauncher(uow_factory))

      config = service._load_llm_config()

      self.assertEqual(config.provider, "gemini")
      self.assertEqual(config.model, "gemini-2.5-flash")
      self.assertEqual(config.api_key, "gemini-secret")
      self.assertEqual(config.base_url, "https://gemini.example/v1beta")
      self.assertEqual(config.timeout_seconds, 22)
    finally:
      conn.close()

  def test_config_center_preserves_masked_nested_api_keys(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      service = ConfigCenterService(uow_factory)
      service.update_section(
        "llm",
        {
          "providers": [
            {
              "provider_id": "openai_default",
              "name": "OpenAI",
              "kind": "openai-compatible",
              "api_key": "secret-value",
              "models": [{"model_id": "gpt-test", "enabled": True}],
            }
          ]
        },
        merge=False,
      )

      masked = service.get_section("llm").data
      saved = service.update_section("llm", masked, merge=False)
      revealed = service.get_section("llm", reveal_sensitive=True).data

      self.assertEqual(masked["providers"][0]["api_key"], "***")
      self.assertEqual(saved.data["providers"][0]["api_key"], "***")
      self.assertEqual(revealed["providers"][0]["api_key"], "secret-value")
    finally:
      conn.close()

  @staticmethod
  def _request_json(
    handler,
    method: str,
    path: str,
    payload: dict[str, object] | None = None,
  ) -> dict[str, object]:
    response = _dispatch_fake_request(handler, method, path, payload)
    return json.loads(response.decode("utf-8"))

  @staticmethod
  def _request_ndjson(handler, path: str) -> list[dict[str, object]]:
    body = _dispatch_fake_request(handler, "GET", path).decode("utf-8")
    return [json.loads(line) for line in body.splitlines() if line.strip()]


if __name__ == "__main__":
  unittest.main()


class _FakeSocket:
  def __init__(self, request: bytes) -> None:
    self.input = io.BytesIO(request)
    self.output = io.BytesIO()

  def makefile(self, mode: str, buffering: int | None = None):
    if "r" in mode:
      return self.input
    return self.output

  def sendall(self, data: bytes) -> None:
    self.output.write(data)

  def close(self) -> None:
    return


def _dispatch_fake_request(
  handler,
  method: str,
  path: str,
  payload: dict[str, object] | bytes | None = None,
) -> bytes:
  _, _, body = _dispatch_fake_request_with_headers(handler, method, path, payload)
  return body


def _dispatch_fake_request_with_headers(
  handler,
  method: str,
  path: str,
  payload: dict[str, object] | bytes | None = None,
  headers: dict[str, str] | None = None,
) -> tuple[str, dict[str, str], bytes]:
  body = b""
  if isinstance(payload, bytes):
    body = payload
  elif payload is not None:
    body = json.dumps(payload).encode("utf-8")
  header_lines = [
    f"{method} {path} HTTP/1.1",
    "Host: test",
    "Connection: close",
    f"Content-Length: {len(body)}",
    "Content-Type: application/json",
  ]
  for key, value in (headers or {}).items():
    header_lines.append(f"{key}: {value}")
  request = (
    "\r\n".join(header_lines) + "\r\n\r\n"
  ).encode("ascii") + body
  fake_socket = _FakeSocket(request)
  handler(fake_socket, ("127.0.0.1", 0), object())
  raw = fake_socket.output.getvalue()
  head, _, response_body = raw.partition(b"\r\n\r\n")
  lines = head.decode("iso-8859-1").split("\r\n")
  response_headers: dict[str, str] = {}
  for line in lines[1:]:
    if ":" not in line:
      continue
    key, value = line.split(":", 1)
    response_headers[key] = value.strip()
  return lines[0], response_headers, response_body
