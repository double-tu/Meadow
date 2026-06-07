import asyncio
import sys
import unittest

from agent_kernel.capabilities import CapabilityCallContext, CapabilityRegistry, CapabilityRuntime
from agent_kernel.capabilities.adapters import LocalToolExecutor, ProcessToolExecutor
from agent_kernel.domain.capability import CapabilitySpec, SideEffectLevel
from agent_kernel.persistence import UnitOfWork, connect_sqlite
from agent_kernel.policy import PolicyEngine
from agent_kernel.runtime import unit_of_work_factory


class ToolCallControlIntegrationTests(unittest.IsolatedAsyncioTestCase):
  async def test_cancel_running_process_tool_call(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      registry = CapabilityRegistry()
      registry.register(
        CapabilitySpec(
          capability_id="proc.long",
          name="long process",
          kind="tool",
          input_schema={},
          output_schema={},
          side_effect_level=SideEffectLevel.NONE,
        )
      )
      process_tools = ProcessToolExecutor()
      process_tools.register(
        "proc.long",
        [sys.executable, "-c", "import time; time.sleep(5)"],
        timeout_seconds=10,
      )
      runtime = CapabilityRuntime(
        registry,
        PolicyEngine(),
        LocalToolExecutor(),
        process_tools=process_tools,
        uow_factory=uow_factory,
      )

      task = asyncio.create_task(
        runtime.call("proc.long", {}, CapabilityCallContext(run_id="run_cancel_tool"))
      )
      tool_call_id = None
      for _ in range(50):
        await asyncio.sleep(0.01)
        with UnitOfWork(conn) as uow:
          calls = uow.tool_calls.list_by_run("run_cancel_tool")
        if calls:
          tool_call_id = calls[0].tool_call_id
          break
      self.assertIsNotNone(tool_call_id)

      cancelled = await runtime.cancel_tool_call(tool_call_id, grace_seconds=1)
      outcome = await task

      self.assertIn(cancelled.status, {"cancelled", "killed"})
      self.assertFalse(outcome.result.ok)
      self.assertIn(outcome.result.error["type"], {"cancelled", "killed"})

      with UnitOfWork(conn) as uow:
        final = uow.tool_calls.get(tool_call_id)

      self.assertIn(final.status, {"cancelled", "killed"})
    finally:
      conn.close()

  async def test_kill_running_process_tool_call(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      registry = CapabilityRegistry()
      registry.register(
        CapabilitySpec(
          capability_id="proc.long",
          name="long process",
          kind="tool",
          input_schema={},
          output_schema={},
          side_effect_level=SideEffectLevel.NONE,
        )
      )
      process_tools = ProcessToolExecutor()
      process_tools.register(
        "proc.long",
        [sys.executable, "-c", "import time; time.sleep(5)"],
        timeout_seconds=10,
      )
      runtime = CapabilityRuntime(
        registry,
        PolicyEngine(),
        LocalToolExecutor(),
        process_tools=process_tools,
        uow_factory=uow_factory,
      )

      task = asyncio.create_task(
        runtime.call("proc.long", {}, CapabilityCallContext(run_id="run_kill_tool"))
      )
      tool_call_id = None
      for _ in range(50):
        await asyncio.sleep(0.01)
        with UnitOfWork(conn) as uow:
          calls = uow.tool_calls.list_by_run("run_kill_tool")
        if calls:
          tool_call_id = calls[0].tool_call_id
          break
      self.assertIsNotNone(tool_call_id)

      killed = await runtime.kill_tool_call(tool_call_id)
      outcome = await task

      self.assertEqual(killed.status, "killed")
      self.assertFalse(outcome.result.ok)
      self.assertEqual(outcome.result.error["type"], "killed")

      with UnitOfWork(conn) as uow:
        final = uow.tool_calls.get(tool_call_id)

      self.assertEqual(final.status, "killed")
    finally:
      conn.close()


if __name__ == "__main__":
  unittest.main()
