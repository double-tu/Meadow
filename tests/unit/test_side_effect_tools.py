from datetime import timedelta
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_kernel.capabilities import CapabilityCallContext, CapabilityRegistry, CapabilityRuntime
from agent_kernel.capabilities.adapters import (
  HTTPResponse,
  LocalFileWorkspace,
  LocalToolExecutor,
  ProcessToolExecutor,
  SideEffectToolProvider,
  UrllibHTTPClient,
)
from agent_kernel.domain.base import utc_now
from agent_kernel.domain.capability import CapabilityGrant, CapabilitySpec, SideEffectLevel
from agent_kernel.observability import MemoryAuditSink
from agent_kernel.persistence import UnitOfWork, connect_sqlite
from agent_kernel.policy import PolicyDecisionType, PolicyEngine
from agent_kernel.runtime import unit_of_work_factory


class SideEffectToolTests(unittest.IsolatedAsyncioTestCase):
  async def test_file_write_runs_through_policy_audit_and_tool_call(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      conn = connect_sqlite()
      try:
        path = str(Path(tmp) / "note.txt")
        registry = CapabilityRegistry()
        registry.register(
          CapabilitySpec(
            capability_id="fs.write_text",
            name="write text",
            kind="tool",
            input_schema={},
            output_schema={},
            side_effect_level=SideEffectLevel.WRITE,
            required_grant="fs.write",
          )
        )
        tools = LocalToolExecutor()
        SideEffectToolProvider(file_workspace=LocalFileWorkspace([tmp])).register(tools)
        grant = CapabilityGrant(
          grant_id="grant_fs",
          capability_id="fs.write_text",
          run_id="run_fs",
          expires_at=utc_now() + timedelta(minutes=5),
          filesystem_scope=[tmp],
        )
        audit_sink = MemoryAuditSink()
        runtime = CapabilityRuntime(
          registry,
          PolicyEngine(grants=[grant]),
          tools,
          audit_sink=audit_sink,
          uow_factory=unit_of_work_factory(conn),
        )

        outcome = await runtime.call(
          "fs.write_text",
          {"path": path, "content": "hello"},
          CapabilityCallContext(run_id="run_fs", idempotency_key="write-note"),
        )

        with UnitOfWork(conn) as uow:
          calls = uow.tool_calls.list_by_run("run_fs")
          audit = uow.audit.list_by_run("run_fs")

        self.assertTrue(outcome.result.ok)
        self.assertEqual(Path(path).read_text(encoding="utf-8"), "hello")
        self.assertEqual(outcome.decision.type, PolicyDecisionType.ALLOW)
        self.assertEqual(calls[0].status, "succeeded")
        self.assertEqual(calls[0].idempotency_key, "write-note")
        self.assertEqual(audit[0].decision, "allow")
        self.assertEqual(audit_sink.records[1].payload["result"]["status"], "succeeded")
      finally:
        conn.close()

  async def test_file_write_denies_path_outside_grant_scope(self) -> None:
    with tempfile.TemporaryDirectory() as allowed:
      with tempfile.TemporaryDirectory() as denied:
        path = str(Path(denied) / "note.txt")
        registry = CapabilityRegistry()
        registry.register(
          CapabilitySpec(
            capability_id="fs.write_text",
            name="write text",
            kind="tool",
            input_schema={},
            output_schema={},
            side_effect_level=SideEffectLevel.WRITE,
            required_grant="fs.write",
          )
        )
        tools = LocalToolExecutor()
        SideEffectToolProvider(file_workspace=LocalFileWorkspace([allowed, denied])).register(tools)
        grant = CapabilityGrant(
          grant_id="grant_fs",
          capability_id="fs.write_text",
          run_id="run_fs",
          expires_at=utc_now() + timedelta(minutes=5),
          filesystem_scope=[allowed],
        )
        runtime = CapabilityRuntime(registry, PolicyEngine(grants=[grant]), tools)

        outcome = await runtime.call(
          "fs.write_text",
          {"path": path, "content": "blocked"},
          CapabilityCallContext(run_id="run_fs"),
        )

        self.assertFalse(outcome.result.ok)
        self.assertEqual(outcome.decision.type, PolicyDecisionType.DENY)
        self.assertEqual(outcome.result.error["type"], "policy_denied")
        self.assertFalse(Path(path).exists())

  async def test_http_request_runs_through_network_scope_policy(self) -> None:
    registry = CapabilityRegistry()
    registry.register(
      CapabilitySpec(
        capability_id="net.http_request",
        name="HTTP request",
        kind="tool",
        input_schema={},
        output_schema={},
        side_effect_level=SideEffectLevel.NETWORK,
        required_grant="net.http",
      )
    )
    tools = LocalToolExecutor()
    client = _FakeHTTPClient()
    SideEffectToolProvider(http_client=client).register(tools)
    grant = CapabilityGrant(
      grant_id="grant_net",
      capability_id="net.http_request",
      run_id="run_net",
      expires_at=utc_now() + timedelta(minutes=5),
      network_scope=["api.example.test"],
    )
    runtime = CapabilityRuntime(registry, PolicyEngine(grants=[grant]), tools)

    allowed = await runtime.call(
      "net.http_request",
      {"method": "GET", "url": "https://api.example.test/status"},
      CapabilityCallContext(run_id="run_net"),
    )
    denied = await runtime.call(
      "net.http_request",
      {"method": "GET", "url": "https://evil.example.test/status"},
      CapabilityCallContext(run_id="run_net"),
    )

    self.assertTrue(allowed.result.ok)
    self.assertEqual(allowed.result.output["body"], "ok")
    self.assertEqual(client.requests[0][1], "https://api.example.test/status")
    self.assertEqual(denied.decision.type, PolicyDecisionType.DENY)
    self.assertFalse(denied.result.ok)
    self.assertEqual(len(client.requests), 1)

  async def test_urllib_http_client_encodes_non_ascii_urls(self) -> None:
    captured = {}

    def fake_urlopen(request, timeout=None):
      captured["url"] = request.full_url
      return _FakeUrlopenResponse()

    with mock.patch("agent_kernel.capabilities.adapters.side_effect.urlopen", fake_urlopen):
      response = UrllibHTTPClient().request("GET", "https://example.test/search?q=大模型 agent 记忆")

    self.assertEqual(
      captured["url"],
      "https://example.test/search?q=%E5%A4%A7%E6%A8%A1%E5%9E%8B%20agent%20%E8%AE%B0%E5%BF%86",
    )
    self.assertEqual(response.body, "ok")

  async def test_process_command_execution_runs_through_policy_audit_and_tool_call(self) -> None:
    conn = connect_sqlite()
    try:
      registry = CapabilityRegistry()
      registry.register(
        CapabilitySpec(
          capability_id="proc.echo",
          name="process echo",
          kind="tool",
          input_schema={},
          output_schema={},
          side_effect_level=SideEffectLevel.EXEC,
        )
      )
      process_tools = ProcessToolExecutor()
      process_tools.register("proc.echo", [sys.executable, "-c", "print('exec-ok')"], timeout_seconds=5)
      grant = CapabilityGrant(
        grant_id="grant_exec",
        capability_id="proc.echo",
        run_id="run_exec",
        expires_at=utc_now() + timedelta(minutes=5),
      )
      runtime = CapabilityRuntime(
        registry,
        PolicyEngine(grants=[grant]),
        LocalToolExecutor(),
        process_tools=process_tools,
        uow_factory=unit_of_work_factory(conn),
      )

      outcome = await runtime.call("proc.echo", {}, CapabilityCallContext(run_id="run_exec"))

      with UnitOfWork(conn) as uow:
        calls = uow.tool_calls.list_by_run("run_exec")
        audit = uow.audit.list_by_run("run_exec")

      self.assertTrue(outcome.result.ok)
      self.assertIn("exec-ok", outcome.result.output["stdout"])
      self.assertEqual(calls[0].status, "succeeded")
      self.assertEqual([record.decision for record in audit], ["allow", "result"])
    finally:
      conn.close()


class _FakeHTTPClient:
  def __init__(self) -> None:
    self.requests: list[tuple[str, str]] = []

  def request(
    self,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: str | None = None,
    timeout_seconds: float | None = None,
  ) -> HTTPResponse:
    self.requests.append((method, url))
    return HTTPResponse(status=200, headers={"content-type": "text/plain"}, body="ok", url=url)


class _FakeUrlopenResponse:
  status = 200

  def __enter__(self):
    return self

  def __exit__(self, exc_type, exc, traceback) -> None:
    return None

  def read(self) -> bytes:
    return b"ok"

  def geturl(self) -> str:
    return "https://example.test/search"

  @property
  def headers(self):
    return self

  def get_content_charset(self) -> str:
    return "utf-8"

  def items(self):
    return [("content-type", "text/plain")]


if __name__ == "__main__":
  unittest.main()
