"""Supervisor-worker orchestration helpers."""

from agent_kernel.agents.mailbox import build_mailbox_message
from agent_kernel.agents.session import AgentSessionService
from agent_kernel.domain.agent import AgentSession, AgentSpec
from agent_kernel.domain.agent import AgentTaskResult
from agent_kernel.domain.states import AgentStatus


class SupervisorService:
  def __init__(self, uow_factory, sessions: AgentSessionService) -> None:
    self._uow_factory = uow_factory
    self._sessions = sessions

  def spawn_child(
    self,
    parent_session_id: str,
    child_spec: AgentSpec,
    task: dict[str, object],
  ) -> AgentSession:
    child = self._sessions.create_session(
      child_spec,
      parent_session_id=parent_session_id,
      task_id=str(task.get("task_id")) if task.get("task_id") is not None else None,
    )
    with self._uow_factory() as uow:
      uow.mailbox.send(
        build_mailbox_message(
          recipient_session_id=child.session_id,
          sender_session_id=parent_session_id,
          mailbox_id=child.mailbox_id,
          content={"type": "task.assigned", "task": task},
        )
      )
    return child

  def await_child(self, child_session_id: str) -> AgentStatus:
    child = self._sessions.get(child_session_id)
    if child is None:
      raise KeyError(f"Child agent session not found: {child_session_id}")
    return child.status

  def get_child_result(self, child_session_id: str) -> AgentTaskResult | None:
    with self._uow_factory() as uow:
      return uow.agent_results.latest_for_session(child_session_id)

  def cancel_child(self, child_session_id: str) -> AgentSession:
    return self._sessions.cancel(child_session_id)

  def cancel_children(self, parent_session_id: str) -> list[AgentSession]:
    with self._uow_factory() as uow:
      children = uow.agent_sessions.list_children(parent_session_id)
    return [self._sessions.cancel(child.session_id) for child in children]
