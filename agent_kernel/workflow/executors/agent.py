"""Agent node executor."""

from agent_kernel.agents.loop import AgentLoop
from agent_kernel.agents.mailbox import build_mailbox_message
from agent_kernel.agents.session import AgentSessionService, oneshot_agent_spec
from agent_kernel.domain.workflow import NodeContext, NodeResult


class AgentNodeExecutor:
  def __init__(
    self,
    uow_factory,
    sessions: AgentSessionService,
    agent_loop: AgentLoop,
    agent_id: str,
    model_ref: str,
  ) -> None:
    self._uow_factory = uow_factory
    self._sessions = sessions
    self._agent_loop = agent_loop
    self._agent_id = agent_id
    self._model_ref = model_ref

  async def execute(self, ctx: NodeContext) -> NodeResult:
    spec = oneshot_agent_spec(
      agent_id=self._agent_id,
      model_ref=self._model_ref,
      name=f"agent-node-{ctx.node.node_id}",
    )
    session = self._sessions.create_session(spec, task_id=str(ctx.input.get("task_id")) if ctx.input.get("task_id") else None)
    with self._uow_factory() as uow:
      uow.mailbox.send(
        build_mailbox_message(
          recipient_session_id=session.session_id,
          mailbox_id=session.mailbox_id,
          content={"type": "workflow.agent_node", "node_id": ctx.node.node_id, "input": ctx.input},
        )
      )
    turn = await self._agent_loop.run_once(session.session_id, self._model_ref)
    state_patch = {"agent_session_id": session.session_id}
    if turn.result is not None:
      state_patch["agent_result"] = turn.result.output
    return NodeResult(state_patch=state_patch, command=turn.command)

