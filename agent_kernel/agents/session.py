"""Agent session service."""

from dataclasses import replace

from agent_kernel.domain.agent import AgentMode, AgentSession, AgentSpec
from agent_kernel.domain.base import new_id, utc_now
from agent_kernel.domain.events import RuntimeEvent, RuntimeEventType
from agent_kernel.domain.states import AgentStatus, assert_transition
from agent_kernel.persistence.unit_of_work import UnitOfWork


class AgentSessionService:
  def __init__(self, uow_factory) -> None:
    self._uow_factory = uow_factory

  def create_session(
    self,
    spec: AgentSpec,
    parent_session_id: str | None = None,
    task_id: str | None = None,
  ) -> AgentSession:
    session = AgentSession(
      session_id=new_id("agent_session"),
      agent_id=spec.agent_id,
      status=AgentStatus.STARTING,
      parent_session_id=parent_session_id,
      task_id=task_id,
      mailbox_id=None,
    )
    session = replace(session, mailbox_id=f"mailbox_{session.session_id}")
    with self._uow_factory() as uow:
      uow.agent_sessions.save(session)
      uow.events.append(
        RuntimeEvent(
          event_type=RuntimeEventType.AGENT_SESSION_CREATED,
          run_id=f"agent:{session.session_id}",
          agent_id=session.agent_id,
          payload={
            "session_id": session.session_id,
            "parent_session_id": parent_session_id,
            "task_id": task_id,
          },
        )
      )
    return session

  def mark_running(self, session_id: str) -> AgentSession:
    with self._uow_factory() as uow:
      session = self._get_existing(uow, session_id)
      if session.status is AgentStatus.STARTING:
        assert_transition(session.status, AgentStatus.RUNNING)
      session = replace(session, status=AgentStatus.RUNNING, updated_at=utc_now())
      uow.agent_sessions.save(session)
      return session

  def complete(self, session_id: str) -> AgentSession:
    with self._uow_factory() as uow:
      session = self._get_existing(uow, session_id)
      if session.status is not AgentStatus.COMPLETED:
        assert_transition(session.status, AgentStatus.COMPLETED)
      session = replace(session, status=AgentStatus.COMPLETED, updated_at=utc_now())
      uow.agent_sessions.save(session)
      return session

  def cancel(self, session_id: str) -> AgentSession:
    with self._uow_factory() as uow:
      session = self._get_existing(uow, session_id)
      if session.status is not AgentStatus.CANCELLED:
        assert_transition(session.status, AgentStatus.CANCELLED)
      session = replace(session, status=AgentStatus.CANCELLED, updated_at=utc_now())
      uow.agent_sessions.save(session)
      uow.events.append(
        RuntimeEvent(
          event_type=RuntimeEventType.AGENT_SESSION_CANCELLED,
          run_id=f"agent:{session.session_id}",
          agent_id=session.agent_id,
          payload={"session_id": session.session_id},
        )
      )
      return session

  def get(self, session_id: str) -> AgentSession | None:
    with self._uow_factory() as uow:
      return uow.agent_sessions.get(session_id)

  @staticmethod
  def _get_existing(uow: UnitOfWork, session_id: str) -> AgentSession:
    session = uow.agent_sessions.get(session_id)
    if session is None:
      raise KeyError(f"Agent session not found: {session_id}")
    return session


def oneshot_agent_spec(agent_id: str, model_ref: str, name: str = "oneshot") -> AgentSpec:
  return AgentSpec(agent_id=agent_id, name=name, mode=AgentMode.ONESHOT, model_ref=model_ref)
