import unittest
import sys

from agent_kernel.capabilities import CapabilityRegistry, CapabilityRuntime
from agent_kernel.capabilities.adapters import LocalToolExecutor, ProcessToolExecutor
from agent_kernel.domain import EdgeSpec, NodeSpec, RunStatus, RuntimeEventType, WorkflowSpec
from agent_kernel.domain.capability import CapabilitySpec, SideEffectLevel, ToolResult
from agent_kernel.persistence import UnitOfWork, connect_sqlite
from agent_kernel.policy import PolicyEngine
from agent_kernel.policy import ApprovalService
from agent_kernel.runtime import RuntimeEngine, unit_of_work_factory
from agent_kernel.workflow import FunctionNodeExecutor, NodeExecutorRegistry, ToolNodeExecutor
from agent_kernel.domain.workflow import ExecutionCommand, NodeResult


def build_tool_workflow(side_effect_level: SideEffectLevel = SideEffectLevel.NONE) -> tuple[WorkflowSpec, CapabilityRuntime]:
  registry = CapabilityRegistry()
  registry.register(
    CapabilitySpec(
      capability_id="tool.echo",
      name="echo",
      kind="tool",
      input_schema={},
      output_schema={},
      side_effect_level=side_effect_level,
    )
  )
  tools = LocalToolExecutor()
  tools.register("tool.echo", lambda input: ToolResult(ok=True, output={"echo": input["text"]}))
  runtime = CapabilityRuntime(registry, PolicyEngine(), tools)
  workflow = WorkflowSpec(
    workflow_id="wf_tool",
    version="0.1.0",
    name="tool workflow",
    input_schema={},
    output_schema={},
    nodes=[
      NodeSpec(node_id="call_tool", kind="tool", capability_ref="tool.echo"),
      NodeSpec(node_id="finish", kind="finish"),
    ],
    edges=[EdgeSpec(from_node="call_tool", to_node="finish")],
    start_node_id="call_tool",
  )
  return workflow, runtime


class ToolWorkflowNodeIntegrationTests(unittest.IsolatedAsyncioTestCase):
  async def test_tool_node_runs_through_capability_runtime(self) -> None:
    conn = connect_sqlite()
    try:
      workflow, capability_runtime = build_tool_workflow()
      registry = NodeExecutorRegistry()
      registry.register("tool", lambda: ToolNodeExecutor(capability_runtime))
      registry.register(
        "finish",
        lambda: FunctionNodeExecutor(lambda ctx: NodeResult(command=ExecutionCommand(type="finish"))),
      )
      engine = RuntimeEngine(unit_of_work_factory(conn), registry)

      created = engine.create_run(workflow, input={"text": "hello"}, run_id="run_tool")
      completed = await engine.run_until_waiting(workflow, created.run_id)

      self.assertEqual(completed.status, RunStatus.COMPLETED)
      self.assertEqual(completed.variables["echo"], "hello")
    finally:
      conn.close()

  async def test_high_risk_tool_node_interrupts_for_approval(self) -> None:
    conn = connect_sqlite()
    try:
      workflow, capability_runtime = build_tool_workflow(SideEffectLevel.EXEC)
      registry = NodeExecutorRegistry()
      approval_service = ApprovalService(unit_of_work_factory(conn))
      registry.register("tool", lambda: ToolNodeExecutor(capability_runtime, approval_service))
      engine = RuntimeEngine(unit_of_work_factory(conn), registry)

      created = engine.create_run(workflow, input={"text": "hello"}, run_id="run_tool_approval")
      interrupted = await engine.run_until_waiting(workflow, created.run_id)

      self.assertEqual(interrupted.status, RunStatus.INTERRUPTED)
      self.assertEqual(interrupted.current_node_id, "call_tool")

      with UnitOfWork(conn) as uow:
        events = uow.events.list_by_run("run_tool_approval")
        approvals = uow.approvals.list_pending("run_tool_approval")

      self.assertEqual(events[-1].event_type, RuntimeEventType.STEP_COMPLETED)
      self.assertEqual(events[-1].payload["command"]["type"], "request_approval")
      self.assertEqual(len(approvals), 1)
      self.assertEqual(approvals[0].target_id, "tool.echo")
    finally:
      conn.close()

  async def test_approved_high_risk_tool_resumes_and_executes(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      registry_cap = CapabilityRegistry()
      registry_cap.register(
        CapabilitySpec(
          capability_id="tool.echo",
          name="echo",
          kind="tool",
          input_schema={},
          output_schema={},
          side_effect_level=SideEffectLevel.EXEC,
        )
      )
      tools = LocalToolExecutor()
      tools.register("tool.echo", lambda input: ToolResult(ok=True, output={"echo": input["text"]}))
      def list_grants(run_id: str):
        with UnitOfWork(conn) as uow:
          return uow.grants.list_for_run(run_id)

      policy = PolicyEngine(grants_provider=list_grants)
      capability_runtime = CapabilityRuntime(registry_cap, policy, tools, uow_factory=uow_factory)
      approval_service = ApprovalService(uow_factory)
      workflow = WorkflowSpec(
        workflow_id="wf_tool_resume",
        version="0.1.0",
        name="tool workflow resume",
        input_schema={},
        output_schema={},
        nodes=[
          NodeSpec(node_id="call_tool", kind="tool", capability_ref="tool.echo"),
          NodeSpec(node_id="finish", kind="finish"),
        ],
        edges=[EdgeSpec(from_node="call_tool", to_node="finish")],
        start_node_id="call_tool",
      )
      registry = NodeExecutorRegistry()
      registry.register("tool", lambda: ToolNodeExecutor(capability_runtime, approval_service))
      registry.register(
        "finish",
        lambda: FunctionNodeExecutor(lambda ctx: NodeResult(command=ExecutionCommand(type="finish"))),
      )
      engine = RuntimeEngine(uow_factory, registry)

      created = engine.create_run(workflow, input={"text": "hello"}, run_id="run_tool_resume")
      interrupted = await engine.run_until_waiting(workflow, created.run_id)

      with UnitOfWork(conn) as uow:
        approval = uow.approvals.list_pending("run_tool_resume")[0]

      approval_service.approve(approval.approval_id)
      engine.resume_from_checkpoint(created.run_id)
      completed = await engine.run_until_waiting(workflow, created.run_id)

      self.assertEqual(interrupted.status, RunStatus.INTERRUPTED)
      self.assertEqual(completed.status, RunStatus.COMPLETED)
      self.assertEqual(completed.variables["echo"], "hello")

      with UnitOfWork(conn) as uow:
        audit = uow.audit.list_by_run("run_tool_resume")

      self.assertIn("require_approval", [record.decision for record in audit])
      self.assertIn("allow", [record.decision for record in audit])
    finally:
      conn.close()

  async def test_process_tool_node_timeout_fails_workflow(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      registry_cap = CapabilityRegistry()
      registry_cap.register(
        CapabilitySpec(
          capability_id="proc.sleep",
          name="sleep",
          kind="tool",
          input_schema={},
          output_schema={},
          side_effect_level=SideEffectLevel.NONE,
        )
      )
      process_tools = ProcessToolExecutor()
      process_tools.register(
        "proc.sleep",
        [sys.executable, "-c", "import time; time.sleep(2)"],
        timeout_seconds=0.05,
      )
      capability_runtime = CapabilityRuntime(
        registry_cap,
        PolicyEngine(),
        LocalToolExecutor(),
        process_tools=process_tools,
        uow_factory=uow_factory,
      )
      workflow = WorkflowSpec(
        workflow_id="wf_process_timeout",
        version="0.1.0",
        name="process timeout workflow",
        input_schema={},
        output_schema={},
        nodes=[NodeSpec(node_id="call_proc", kind="tool", capability_ref="proc.sleep")],
        edges=[],
        start_node_id="call_proc",
      )
      registry = NodeExecutorRegistry()
      registry.register("tool", lambda: ToolNodeExecutor(capability_runtime))
      engine = RuntimeEngine(uow_factory, registry)

      created = engine.create_run(workflow, input={}, run_id="run_proc_timeout")
      failed = await engine.run_until_waiting(workflow, created.run_id)

      self.assertEqual(failed.status, RunStatus.FAILED)

      with UnitOfWork(conn) as uow:
        calls = uow.tool_calls.list_by_run("run_proc_timeout")

      self.assertEqual(calls[0].status, "failed")
      self.assertEqual(calls[0].error["type"], "timeout")
    finally:
      conn.close()


if __name__ == "__main__":
  unittest.main()
