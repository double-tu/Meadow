"""Daily Agent execution boundary for conversation hosts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from agent_kernel.agents import ContinuousAgentRunner, ContinuousRunnerConfig, ContinuousRunnerResult
from agent_kernel.autonomy.builtin_skills import ensure_builtin_atomic_skills
from agent_kernel.autonomy.skill_service import SkillService
from agent_kernel.capabilities.atomic import AtomicToolCatalog
from agent_kernel.capabilities.runtime import CapabilityRuntime
from agent_kernel.context import ContextAssembler
from agent_kernel.memory import MemoryFacade
from agent_kernel.models import ModelGateway


DAILY_AGENT_ID = "desktop_daily_agent"


@dataclass(slots=True)
class DailyAgentRequest:
  session_id: str
  run_id: str
  user_content: str
  history_messages: list[dict[str, Any]] = field(default_factory=list)
  selected_skill_ids: list[str] = field(default_factory=list)
  provider_name: str = "mock"
  model_ref: str = "mock"
  agent_id: str = DAILY_AGENT_ID
  max_turns: int = 16


@dataclass(slots=True)
class DailyAgentResponse:
  content: str
  raw: dict[str, Any]


class DailyAgentExecutor(Protocol):
  async def execute(self, request: DailyAgentRequest, gateway: ModelGateway) -> DailyAgentResponse:
    ...


class ContinuousDailyAgentExecutor:
  """Executes daily-agent turns through the current continuous tool loop.

  This adapter keeps the host-facing chat service decoupled from the concrete
  runner. A durable workflow-backed executor can replace it without changing
  chat/session code.
  """

  def __init__(
    self,
    *,
    uow_factory,
    skill_service: SkillService | None,
    capability_runtime: CapabilityRuntime | None,
    tool_catalog: AtomicToolCatalog | None,
    memory: MemoryFacade | None = None,
  ) -> None:
    self._uow_factory = uow_factory
    self._skill_service = skill_service
    self._capability_runtime = capability_runtime
    self._tool_catalog = tool_catalog
    self._memory = memory or MemoryFacade(uow_factory)

  @property
  def is_configured(self) -> bool:
    return self._skill_service is not None and self._capability_runtime is not None and self._tool_catalog is not None

  async def execute(self, request: DailyAgentRequest, gateway: ModelGateway) -> DailyAgentResponse:
    if self._skill_service is None or self._capability_runtime is None or self._tool_catalog is None:
      raise RuntimeError("Daily Agent executor is not configured.")
    ensure_builtin_atomic_skills(self._skill_service)
    active_skills = self._skill_service.list_active()
    if request.selected_skill_ids:
      selected_id_set = set(request.selected_skill_ids)
      selected = [skill for skill in active_skills if skill.skill_id in selected_id_set]
      active_skills = selected or active_skills
    runner = ContinuousAgentRunner(
      uow_factory=self._uow_factory,
      model_gateway=gateway,
      capability_runtime=self._capability_runtime,
      tool_catalog=self._tool_catalog,
      context_assembler=ContextAssembler(
        uow_factory=self._uow_factory,
        memory=self._memory,
      ),
      skills=active_skills,
    )
    outcome = await runner.run(
      run_id=request.run_id,
      user_message=request.user_content,
      history_messages=request.history_messages,
      task_id=request.session_id,
      agent_id=request.agent_id,
      scope=request.session_id,
      config=ContinuousRunnerConfig(
        provider_name=request.provider_name,
        model_ref=request.model_ref,
        max_turns=request.max_turns,
      ),
    )
    raw = runner_result_to_raw(outcome)
    return DailyAgentResponse(content=assistant_content_from_runner_result(outcome), raw=raw)


def runner_result_to_raw(outcome: ContinuousRunnerResult) -> dict[str, Any]:
  return {
    "daily_agent": {
      "run_id": outcome.run_id,
      "status": outcome.status,
      "turns": outcome.turns,
      "output": outcome.output,
      "pending": outcome.pending,
      "tool_calls": [
        {
          "name": call.name,
          "capability_id": call.capability_id,
          "input": call.input,
          "ok": call.ok,
          "output": call.output,
          "error": call.error,
          "requires_approval": call.requires_approval,
        }
        for call in outcome.tool_calls
      ],
    },
  }


def assistant_content_from_runner_result(outcome: ContinuousRunnerResult) -> str:
  if outcome.status == "waiting_for_user" and isinstance(outcome.pending, dict):
    question = outcome.pending.get("question")
    candidates = outcome.pending.get("candidates")
    text = str(question) if isinstance(question, str) and question.strip() else "我需要你补充一个信息才能继续。"
    if isinstance(candidates, list) and candidates:
      text += "\n\n可选项：" + "、".join(str(item) for item in candidates)
    return text
  if outcome.status == "awaiting_approval" and isinstance(outcome.pending, dict):
    capability_id = outcome.pending.get("capability_id")
    reason = outcome.pending.get("reason")
    return f"需要审批后才能继续执行：{capability_id or '工具调用'}。\n原因：{reason or '策略要求审批'}"
  return assistant_content_from_runner_output(outcome.output)


def assistant_content_from_runner_output(output: dict[str, Any]) -> str:
  content = output.get("content")
  if isinstance(content, str) and content.strip():
    return content.strip()
  summary = output.get("summary")
  if isinstance(summary, str) and summary.strip():
    return summary.strip()
  value = output.get("value")
  if isinstance(value, str) and value.strip():
    return value.strip()
  if output:
    return str(output)
  return "日常 Agent 已完成运行，但没有返回可展示内容。"
