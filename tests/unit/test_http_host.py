import io
import json
import unittest
from datetime import timedelta

from agent_kernel.agents import AgentDelegationBroker, ConnectorTurn, FakeAgentConnector
from agent_kernel.app.control_plane import ControlPlaneService
from agent_kernel.domain import (
  ArtifactRef,
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
from agent_kernel.hosts.http import HTTPHost, make_handler
from agent_kernel.persistence import UnitOfWork, connect_sqlite
from agent_kernel.policy import ApprovalService
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
