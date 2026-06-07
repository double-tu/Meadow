"""Minimal agent loop."""

from dataclasses import replace
import json

from agent_kernel.domain.agent import AgentSession, AgentTaskResult
from agent_kernel.domain.base import new_id, utc_now
from agent_kernel.domain.context import ModelContext
from agent_kernel.domain.events import RuntimeEvent, RuntimeEventType
from agent_kernel.domain.states import AgentStatus, assert_transition
from agent_kernel.domain.workflow import ExecutionCommand
from agent_kernel.models.gateway import ModelGateway
from agent_kernel.persistence.unit_of_work import UnitOfWork
from agent_kernel.agents.skills import SkillContextProvider


class AgentTurnResult:
  def __init__(
    self,
    session: AgentSession,
    result: AgentTaskResult | None,
    command: ExecutionCommand | None,
  ) -> None:
    self.session = session
    self.result = result
    self.command = command


class AgentLoop:
  def __init__(
    self,
    uow_factory,
    model_gateway: ModelGateway,
    provider_name: str = "mock",
    context_manager=None,
    skill_context_provider: SkillContextProvider | None = None,
  ) -> None:
    self._uow_factory = uow_factory
    self._model_gateway = model_gateway
    self._provider_name = provider_name
    self._context_manager = context_manager
    self._skill_context_provider = skill_context_provider

  async def run_once(self, session_id: str, model_ref: str) -> AgentTurnResult:
    with self._uow_factory() as uow:
      session = uow.agent_sessions.get(session_id)
      if session is None:
        raise KeyError(f"Agent session not found: {session_id}")
      messages = uow.mailbox.list_pending(session_id)

    if session.status is AgentStatus.STARTING:
      assert_transition(session.status, AgentStatus.RUNNING)
      session = replace(session, status=AgentStatus.RUNNING, updated_at=utc_now())

    raw_messages = [{"role": "user", "content": message.content} for message in messages]
    selected_skills = []
    if self._skill_context_provider is not None:
      raw_messages, selected_skills = self._skill_context_provider.build_skill_context(session, raw_messages)
    if self._context_manager is None:
      context = ModelContext(messages=raw_messages)
    else:
      context = self._context_manager.build(
        run_id=f"agent:{session.session_id}",
        scope=session.session_id,
        model_ref=model_ref,
        messages=raw_messages,
      )
    result = self._normalize_model_result(await self._model_gateway.complete(self._provider_name, model_ref, context))
    finish = bool(result.get("finish"))
    output = result.get("output", result)
    command = self._parse_command(result.get("command"))
    task_result = None

    with self._uow_factory() as uow:
      for message in messages:
        uow.mailbox.mark_handled(message.message_id)
      if finish:
        if session.status is not AgentStatus.COMPLETED:
          assert_transition(session.status, AgentStatus.COMPLETED)
        session = replace(session, status=AgentStatus.COMPLETED, updated_at=utc_now())
      else:
        session = replace(session, status=AgentStatus.IDLE, updated_at=utc_now())
      uow.agent_sessions.save(session)
      if finish:
        task_result = AgentTaskResult(
          result_id=new_id("agent_result"),
          session_id=session.session_id,
          task_id=session.task_id,
          ok=True,
          output=output if isinstance(output, dict) else {"value": output},
        )
        uow.agent_results.save(task_result)
      uow.events.append(
        RuntimeEvent(
          event_type=RuntimeEventType.AGENT_TURN_COMPLETED,
          run_id=f"agent:{session.session_id}",
          agent_id=session.agent_id,
          payload={
            "session_id": session.session_id,
            "finish": finish,
            "output": output,
            "selected_skill_ids": [skill.skill_id for skill in selected_skills],
          },
        )
      )
    return AgentTurnResult(session=session, result=task_result, command=command)

  @staticmethod
  def _parse_command(raw: object) -> ExecutionCommand | None:
    if raw is None:
      return None
    if not isinstance(raw, dict):
      raise ValueError("Agent command must be a dictionary.")
    return ExecutionCommand.from_dict(raw)

  @staticmethod
  def _normalize_model_result(result: dict[str, object]) -> dict[str, object]:
    if any(key in result for key in ("finish", "output", "command")):
      return result
    content = result.get("content")
    if not isinstance(content, str):
      return result
    try:
      parsed = json.loads(content)
    except json.JSONDecodeError:
      return result
    if not isinstance(parsed, dict):
      return result
    normalized = dict(parsed)
    normalized.setdefault("raw_content", content)
    return normalized
