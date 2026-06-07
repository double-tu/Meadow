import unittest

from agent_kernel.capabilities import CapabilityRegistry, CapabilityRuntime
from agent_kernel.capabilities.adapters import LocalToolExecutor
from agent_kernel.domain import EdgeSpec, NodeSpec, RunStatus, WorkflowSpec
from agent_kernel.domain.capability import CapabilitySpec, SideEffectLevel, ToolResult
from agent_kernel.domain.workflow import ExecutionCommand, NodeResult
from agent_kernel.evaluation import ReplayAssertions, ReplayEvalSuite, ReplayService
from agent_kernel.observability import TraceService
from agent_kernel.persistence import connect_sqlite
from agent_kernel.policy import PolicyEngine
from agent_kernel.runtime import RuntimeEngine, unit_of_work_factory
from agent_kernel.workflow import FunctionNodeExecutor, NodeExecutorRegistry, ToolNodeExecutor


class ReplayIntegrationTests(unittest.IsolatedAsyncioTestCase):
  async def test_exact_replay_uses_persisted_trace_without_reexecuting_tool(self) -> None:
    conn = connect_sqlite()
    try:
      calls = {"count": 0}
      uow_factory = unit_of_work_factory(conn)
      capabilities = CapabilityRegistry()
      capabilities.register(
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
      tools.register(
        "tool.echo",
        lambda input: calls.__setitem__("count", calls["count"] + 1)
        or ToolResult(ok=True, output={"echo": input["text"]}),
      )
      capability_runtime = CapabilityRuntime(
        capabilities,
        PolicyEngine(),
        tools,
        uow_factory=uow_factory,
      )
      registry = NodeExecutorRegistry()
      registry.register("tool", lambda: ToolNodeExecutor(capability_runtime))
      registry.register(
        "finish",
        lambda: FunctionNodeExecutor(lambda ctx: NodeResult(command=ExecutionCommand(type="finish"))),
      )
      workflow = WorkflowSpec(
        workflow_id="wf_replay",
        version="0.1.0",
        name="replay workflow",
        input_schema={},
        output_schema={},
        nodes=[
          NodeSpec(node_id="tool", kind="tool", capability_ref="tool.echo"),
          NodeSpec(node_id="finish", kind="finish"),
        ],
        edges=[EdgeSpec(from_node="tool", to_node="finish")],
        start_node_id="tool",
      )
      engine = RuntimeEngine(uow_factory, registry)

      created = engine.create_run(workflow, input={"text": "hello"}, run_id="run_replay")
      completed = await engine.run_until_waiting(workflow, created.run_id)
      replay = ReplayService(uow_factory).exact_replay("run_replay")
      partial = ReplayService(uow_factory).partial_replay("run_replay", replay.events[0].event_id)
      recovery = ReplayService(uow_factory).recovery_replay("run_replay")
      suite = ReplayEvalSuite().evaluate_completed_run(replay)
      timeline = TraceService(uow_factory).build_timeline("run_replay")

      self.assertEqual(completed.status, RunStatus.COMPLETED)
      self.assertEqual(calls["count"], 1)
      self.assertEqual(replay.replayed_external_calls, 0)
      self.assertEqual(replay.final_state.status, RunStatus.COMPLETED)
      self.assertGreaterEqual(len(replay.events), 1)
      self.assertEqual(len(partial.events), 1)
      self.assertEqual(recovery.replayed_external_calls, 0)
      self.assertTrue(ReplayAssertions.event_exists(replay, "run.completed").passed)
      self.assertTrue(suite.passed)
      self.assertGreaterEqual(len(timeline.entries), len(replay.events))
      self.assertEqual(calls["count"], 1)
    finally:
      conn.close()


if __name__ == "__main__":
  unittest.main()
