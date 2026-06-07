import io
import json
import unittest

from agent_kernel.domain import (
  ArtifactRef,
  RunState,
  RunStatus,
  RuntimeEvent,
  RuntimeEventType,
  ToolCallRecord,
  ToolCallStatus,
)
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
  body = b""
  if isinstance(payload, bytes):
    body = payload
  elif payload is not None:
    body = json.dumps(payload).encode("utf-8")
  request = (
    f"{method} {path} HTTP/1.1\r\n"
    "Host: test\r\n"
    "Connection: close\r\n"
    f"Content-Length: {len(body)}\r\n"
    "Content-Type: application/json\r\n"
    "\r\n"
  ).encode("ascii") + body
  fake_socket = _FakeSocket(request)
  handler(fake_socket, ("127.0.0.1", 0), object())
  raw = fake_socket.output.getvalue()
  _, _, body = raw.partition(b"\r\n\r\n")
  return body
