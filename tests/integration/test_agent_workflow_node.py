import unittest

from agent_kernel.agents import AgentLoop, AgentSessionService
from agent_kernel.domain import EdgeSpec, NodeSpec, RunStatus, WorkflowSpec
from agent_kernel.models import MockModelProvider, ModelGateway
from agent_kernel.persistence import connect_sqlite
from agent_kernel.runtime import RuntimeEngine, unit_of_work_factory
from agent_kernel.workflow import AgentNodeExecutor, FunctionNodeExecutor, NodeExecutorRegistry
from agent_kernel.domain.workflow import ExecutionCommand, NodeResult


class AgentWorkflowNodeIntegrationTests(unittest.IsolatedAsyncioTestCase):
  async def test_agent_node_executes_inside_workflow(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      gateway = ModelGateway()
      gateway.register_provider(
        "mock",
        MockModelProvider(responses=[{"finish": True, "output": {"answer": "done"}}]),
      )
      agent_loop = AgentLoop(uow_factory, gateway)
      sessions = AgentSessionService(uow_factory)
      registry = NodeExecutorRegistry()
      registry.register(
        "agent",
        lambda: AgentNodeExecutor(
          uow_factory=uow_factory,
          sessions=sessions,
          agent_loop=agent_loop,
          agent_id="agent_worker",
          model_ref="mock-small",
        ),
      )
      registry.register(
        "finish",
        lambda: FunctionNodeExecutor(
          lambda ctx: NodeResult(command=ExecutionCommand(type="finish"))
        ),
      )
      workflow = WorkflowSpec(
        workflow_id="wf_agent_node",
        version="0.1.0",
        name="agent node workflow",
        input_schema={},
        output_schema={},
        nodes=[
          NodeSpec(node_id="ask_agent", kind="agent"),
          NodeSpec(node_id="finish", kind="finish"),
        ],
        edges=[EdgeSpec(from_node="ask_agent", to_node="finish")],
        start_node_id="ask_agent",
      )
      engine = RuntimeEngine(uow_factory, registry)

      created = engine.create_run(workflow, input={"task_id": "task_1"}, run_id="run_agent_node")
      completed = await engine.run_until_waiting(workflow, created.run_id)

      self.assertEqual(completed.status, RunStatus.COMPLETED)
      self.assertEqual(completed.variables["agent_result"], {"answer": "done"})
      self.assertTrue(completed.variables["agent_session_id"].startswith("agent_session_"))
    finally:
      conn.close()


if __name__ == "__main__":
  unittest.main()
