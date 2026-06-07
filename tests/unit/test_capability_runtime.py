from datetime import timedelta
import unittest

from agent_kernel.capabilities import CapabilityCallContext, CapabilityRegistry, CapabilityRuntime
from agent_kernel.capabilities.adapters import (
  FakeMCPClient,
  FakeWorkbenchClient,
  LocalToolExecutor,
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
    mcp_result = await mcp.call_tool("server", "tool", {"x": 1})

    workbench = FakeWorkbenchClient()
    workbench.register_response("inspect", WorkbenchResult(ok=True, output={"status": "ok"}))
    workbench_result = await workbench.execute(
      WorkbenchCommand(command_id="cmd_1", kind="inspect", payload={})
    )

    self.assertTrue(mcp_result.ok)
    self.assertEqual(mcp.calls[0][2], {"x": 1})
    self.assertTrue(workbench_result.ok)
    self.assertEqual(workbench.commands[0].kind, "inspect")


if __name__ == "__main__":
  unittest.main()
