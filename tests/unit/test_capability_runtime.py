from datetime import timedelta
import unittest

from agent_kernel.capabilities import CapabilityCallContext, CapabilityRegistry, CapabilityRuntime
from agent_kernel.capabilities.adapters import (
  FakeMCPClient,
  FakeWorkbenchClient,
  LocalToolExecutor,
  MCPServerCommand,
  MCPToolExecutor,
  StdioMCPClient,
  WorkbenchCommand,
  WorkbenchResult,
)
from agent_kernel.domain.base import utc_now
from agent_kernel.domain.capability import CapabilityGrant, CapabilitySpec, SideEffectLevel, ToolResult
from agent_kernel.policy import PolicyDecisionType, PolicyEngine


class CapabilityRuntimeTests(unittest.IsolatedAsyncioTestCase):
  async def test_allows_low_risk_tool_call(self) -> None:
    registry = CapabilityRegistry()
    registry.register(
      CapabilitySpec(
        capability_id="tool.echo",
        name="echo",
        kind="tool",
        input_schema={},
        output_schema={},
        side_effect_level=SideEffectLevel.NONE,
      )
    )
    tools = LocalToolExecutor()
    tools.register("tool.echo", lambda input: ToolResult(ok=True, output={"echo": input["text"]}))
    runtime = CapabilityRuntime(registry, PolicyEngine(), tools)

    outcome = await runtime.call("tool.echo", {"text": "hi"}, CapabilityCallContext(run_id="run_1"))

    self.assertTrue(outcome.result.ok)
    self.assertEqual(outcome.result.output, {"echo": "hi"})
    self.assertEqual(outcome.decision.type, PolicyDecisionType.ALLOW)

  async def test_persists_tool_call_success(self) -> None:
    from agent_kernel.persistence import UnitOfWork, connect_sqlite
    from agent_kernel.runtime import unit_of_work_factory

    conn = connect_sqlite()
    try:
      registry = CapabilityRegistry()
      registry.register(
        CapabilitySpec(
          capability_id="tool.echo",
          name="echo",
          kind="tool",
          input_schema={},
          output_schema={},
          side_effect_level=SideEffectLevel.NONE,
        )
      )
      tools = LocalToolExecutor()
      tools.register("tool.echo", lambda input: ToolResult(ok=True, output={"echo": input["text"]}))
      runtime = CapabilityRuntime(
        registry,
        PolicyEngine(),
        tools,
        uow_factory=unit_of_work_factory(conn),
      )

      outcome = await runtime.call(
        "tool.echo",
        {"text": "hi"},
        CapabilityCallContext(run_id="run_1", idempotency_key="idem_1"),
      )

      with UnitOfWork(conn) as uow:
        calls = uow.tool_calls.list_by_run("run_1")

      self.assertTrue(outcome.result.ok)
      self.assertEqual(calls[0].status, "succeeded")
      self.assertEqual(calls[0].idempotency_key, "idem_1")
      self.assertEqual(calls[0].output, {"echo": "hi"})
    finally:
      conn.close()

  async def test_denies_missing_required_grant(self) -> None:
    registry = CapabilityRegistry()
    registry.register(
      CapabilitySpec(
        capability_id="tool.write",
        name="write",
        kind="tool",
        input_schema={},
        output_schema={},
        side_effect_level=SideEffectLevel.WRITE,
        required_grant="fs.write",
      )
    )
    runtime = CapabilityRuntime(registry, PolicyEngine(), LocalToolExecutor())

    outcome = await runtime.call("tool.write", {}, CapabilityCallContext(run_id="run_1"))

    self.assertFalse(outcome.result.ok)
    self.assertEqual(outcome.result.error["type"], "policy_denied")
    self.assertEqual(outcome.decision.type, PolicyDecisionType.DENY)

  async def test_high_risk_exec_requires_approval_without_grant(self) -> None:
    registry = CapabilityRegistry()
    registry.register(
      CapabilitySpec(
        capability_id="tool.exec",
        name="exec",
        kind="tool",
        input_schema={},
        output_schema={},
        side_effect_level=SideEffectLevel.EXEC,
      )
    )
    tools = LocalToolExecutor()
    executed = {"value": False}
    tools.register(
      "tool.exec",
      lambda input: executed.__setitem__("value", True) or ToolResult(ok=True),
    )
    runtime = CapabilityRuntime(registry, PolicyEngine(), tools)

    outcome = await runtime.call("tool.exec", {}, CapabilityCallContext(run_id="run_1"))

    self.assertTrue(outcome.requires_approval)
    self.assertIsNone(outcome.result)
    self.assertFalse(executed["value"])

  async def test_grant_with_approval_required_still_interrupts(self) -> None:
    registry = CapabilityRegistry()
    registry.register(
      CapabilitySpec(
        capability_id="tool.exec",
        name="exec",
        kind="tool",
        input_schema={},
        output_schema={},
        side_effect_level=SideEffectLevel.EXEC,
      )
    )
    grant = CapabilityGrant(
      grant_id="grant_1",
      capability_id="tool.exec",
      run_id="run_1",
      expires_at=utc_now() + timedelta(minutes=5),
      approval_required=True,
    )
    runtime = CapabilityRuntime(registry, PolicyEngine(grants=[grant]), LocalToolExecutor())

    outcome = await runtime.call("tool.exec", {}, CapabilityCallContext(run_id="run_1"))

    self.assertEqual(outcome.decision.type, PolicyDecisionType.REQUIRE_APPROVAL)

  async def test_fake_mcp_and_workbench_adapters(self) -> None:
    mcp = FakeMCPClient()
    mcp.register_response("server", "tool", ToolResult(ok=True, output={"value": 1}))
    mcp.register_tools("server", [{"name": "tool", "description": "fake"}])
    tools = await mcp.list_tools("server")
    mcp_result = await mcp.call_tool("server", "tool", {"x": 1})

    workbench = FakeWorkbenchClient()
    workbench.register_response("inspect", WorkbenchResult(ok=True, output={"status": "ok"}))
    workbench_result = await workbench.execute(
      WorkbenchCommand(command_id="cmd_1", kind="inspect", payload={})
    )

    self.assertEqual(tools[0]["name"], "tool")
    self.assertTrue(mcp_result.ok)
    self.assertEqual(mcp.calls[0][2], {"x": 1})
    self.assertTrue(workbench_result.ok)
    self.assertEqual(workbench.commands[0].kind, "inspect")

  async def test_stdio_mcp_client_lists_and_calls_real_subprocess_server(self) -> None:
    import sys

    client = StdioMCPClient(
      {
        "server": MCPServerCommand(
          argv=[sys.executable, "-u", "-c", _mcp_server_script()],
          startup_timeout_seconds=2,
          request_timeout_seconds=2,
          shutdown_timeout_seconds=1,
        )
      }
    )

    try:
      tools = await client.list_tools("server")
      result = await client.call_tool("server", "echo", {"text": "hello"})
    finally:
      await client.stop_all("test finished")

    self.assertEqual(tools[0]["name"], "echo")
    self.assertTrue(result.ok)
    self.assertEqual(result.output["content"][0]["text"], "hello")

  async def test_mcp_tool_executor_runs_through_capability_runtime(self) -> None:
    from agent_kernel.persistence import UnitOfWork, connect_sqlite
    from agent_kernel.runtime import unit_of_work_factory

    conn = connect_sqlite()
    try:
      registry = CapabilityRegistry()
      registry.register(
        CapabilitySpec(
          capability_id="mcp.echo",
          name="MCP Echo",
          kind="tool",
          input_schema={},
          output_schema={},
          side_effect_level=SideEffectLevel.READ,
        )
      )
      client = FakeMCPClient()
      client.register_response(
        "server",
        "echo",
        ToolResult(ok=True, output={"content": [{"type": "text", "text": "ok"}]}),
      )
      mcp_tools = MCPToolExecutor(client)
      mcp_tools.register("mcp.echo", "server", "echo")
      runtime = CapabilityRuntime(
        registry,
        PolicyEngine(),
        LocalToolExecutor(),
        mcp_tools=mcp_tools,
        uow_factory=unit_of_work_factory(conn),
      )

      outcome = await runtime.call("mcp.echo", {"text": "hello"}, CapabilityCallContext(run_id="run_mcp"))

      with UnitOfWork(conn) as uow:
        calls = uow.tool_calls.list_by_run("run_mcp")
        audits = uow.audit.list_by_run("run_mcp")

      self.assertTrue(outcome.result.ok)
      self.assertEqual(client.calls[0], ("server", "echo", {"text": "hello"}))
      self.assertEqual(calls[0].status, "succeeded")
      self.assertEqual(audits[0].action, "capability.call.policy_check")
    finally:
      conn.close()


if __name__ == "__main__":
  unittest.main()


def _mcp_server_script() -> str:
  return (
    "import json, sys\n"
    "for line in sys.stdin:\n"
    "    frame = json.loads(line)\n"
    "    method = frame.get('method')\n"
    "    if method == 'notifications/initialized':\n"
    "        continue\n"
    "    request_id = frame.get('id')\n"
    "    if method == 'initialize':\n"
    "        result = {'protocolVersion': frame['params']['protocolVersion'], 'capabilities': {'tools': {}}, "
    "'serverInfo': {'name': 'fake-mcp', 'version': '1.0'}}\n"
    "    elif method == 'tools/list':\n"
    "        result = {'tools': [{'name': 'echo', 'description': 'Echo text', "
    "'inputSchema': {'type': 'object'}}]}\n"
    "    elif method == 'tools/call':\n"
    "        text = frame['params']['arguments']['text']\n"
    "        result = {'content': [{'type': 'text', 'text': text}], 'isError': False}\n"
    "    elif method == 'shutdown':\n"
    "        print(json.dumps({'jsonrpc': '2.0', 'id': request_id, 'result': {}}), flush=True)\n"
    "        break\n"
    "    else:\n"
    "        print(json.dumps({'jsonrpc': '2.0', 'id': request_id, "
    "'error': {'code': -32601, 'message': 'not found'}}), flush=True)\n"
    "        continue\n"
    "    print(json.dumps({'jsonrpc': '2.0', 'id': request_id, 'result': result}), flush=True)\n"
  )
