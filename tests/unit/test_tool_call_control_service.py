import asyncio
import sys
import unittest

from agent_kernel.app.tool_call_control import ToolCallControlService
from agent_kernel.capabilities import CapabilityCallContext, CapabilityRegistry, CapabilityRuntime
from agent_kernel.capabilities.adapters import LocalToolExecutor, ProcessToolExecutor
from agent_kernel.domain.capability import CapabilitySpec, SideEffectLevel
from agent_kernel.domain.events import RuntimeEventType
from agent_kernel.persistence import UnitOfWork, connect_sqlite
from agent_kernel.policy import PolicyEngine
from agent_kernel.runtime import unit_of_work_factory


class ToolCallControlServiceTests(unittest.IsolatedAsyncioTestCase):
  async def test_cancel_dispatches_to_runtime_when_available(self) -> None:
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
        runtime.call("proc.long", {}, CapabilityCallContext(run_id="run_control_service"))
      )
      tool_call_id = await self._wait_for_tool_call(conn, "run_control_service")

      outcome = await ToolCallControlService(uow_factory, runtime=runtime).cancel(
        tool_call_id,
        grace_seconds=1,
      )
      await task

      self.assertTrue(outcome.dispatched)
      self.assertIn(outcome.tool_call.status, {"cancelled", "killed"})
      with UnitOfWork(conn) as uow:
        events = uow.events.list_by_run("run_control_service")

      event_types = [event.event_type for event in events]
      self.assertIn(RuntimeEventType.TOOL_CALL_CANCEL_REQUESTED, event_types)
      self.assertTrue(
        RuntimeEventType.TOOL_CALL_CANCELLED in event_types
        or RuntimeEventType.TOOL_CALL_KILLED in event_types
      )
    finally:
      conn.close()

  @staticmethod
  async def _wait_for_tool_call(conn, run_id: str) -> str:
    for _ in range(50):
      await asyncio.sleep(0.01)
      with UnitOfWork(conn) as uow:
        calls = uow.tool_calls.list_by_run(run_id)
      if calls:
        return calls[0].tool_call_id
    raise AssertionError("Timed out waiting for tool call.")
