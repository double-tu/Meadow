"""Desktop chat session service."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
import os
from typing import Any, Protocol

from agent_kernel.app.conversation_task_hub import ConversationTaskHub
from agent_kernel.agents import ContinuousAgentRunner, ContinuousRunnerConfig
from agent_kernel.autonomy.builtin_skills import ensure_builtin_atomic_skills
from agent_kernel.autonomy.skill_service import SkillService
from agent_kernel.capabilities.atomic import AtomicCapabilityProvider
from agent_kernel.capabilities.runtime import CapabilityRuntime
from agent_kernel.config import LLMConfig
from agent_kernel.context import ContextAssembler
from agent_kernel.app.conversation_history import ConversationHistoryCompactor
from agent_kernel.domain.base import DomainModel, new_id, utc_now
from agent_kernel.domain.context import ModelContext
from agent_kernel.domain.states import RunStatus
from agent_kernel.memory import MemoryFacade
from agent_kernel.models import AnthropicMessagesProvider, GeminiProvider, ModelGateway, OpenAICompatibleProvider


class DesktopChatTaskLauncher(Protocol):
  async def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
    ...


class DesktopChatRunControl(Protocol):
  def cancel_run(self, run_id: str, reason: str):
    ...


@dataclass(slots=True)
class DesktopChatSession(DomainModel):
  session_id: str
  title: str
  status: str = "idle"
  message_count: int = 0
  metadata: dict[str, Any] = field(default_factory=dict)
  created_at: datetime = field(default_factory=utc_now)
  updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class DesktopChatMessage(DomainModel):
  message_id: str
  session_id: str
  role: str
  content: str
  run_id: str | None = None
  metadata: dict[str, Any] = field(default_factory=dict)
  created_at: datetime = field(default_factory=utc_now)


class DesktopChatService:
  def __init__(
    self,
    uow_factory,
    launcher: DesktopChatTaskLauncher,
    run_control: DesktopChatRunControl | None = None,
    model_gateway: ModelGateway | None = None,
    model_provider_name: str | None = None,
    model_ref: str | None = None,
    skill_service: SkillService | None = None,
    capability_runtime: CapabilityRuntime | None = None,
    atomic_capabilities: AtomicCapabilityProvider | None = None,
    conversation_task_hub: ConversationTaskHub | None = None,
    default_llm_config: LLMConfig | None = None,
  ) -> None:
    self._uow_factory = uow_factory
    self._launcher = launcher
    self._run_control = run_control
    self._model_gateway = model_gateway
    self._model_provider_name = model_provider_name
    self._model_ref = model_ref
    self._skill_service = skill_service
    self._capability_runtime = capability_runtime
    self._atomic_capabilities = atomic_capabilities
    self._conversation_task_hub = conversation_task_hub
    self._default_llm_config = default_llm_config
    self._memory = MemoryFacade(self._uow_factory)
    self._history_compactor = ConversationHistoryCompactor(self._memory)

  def create_session(self, data: dict[str, Any] | None = None) -> DesktopChatSession:
    raw = data or {}
    session = DesktopChatSession(
      session_id=str(raw.get("session_id") or new_id("chat")),
      title=str(raw.get("title") or "新的对话"),
      metadata=raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {},
    )
    self._save_session(session)
    return session

  def list_sessions(self) -> list[DesktopChatSession]:
    with self._uow_factory() as uow:
      records = uow.interactions.list_records("desktop_chat_session")
    return [DesktopChatSession.from_dict(record) for record in records]

  def get_session(self, session_id: str) -> DesktopChatSession | None:
    with self._uow_factory() as uow:
      record = uow.interactions.get_record("desktop_chat_session", session_id)
    return DesktopChatSession.from_dict(record) if record is not None else None

  def list_messages(self, session_id: str) -> list[DesktopChatMessage]:
    if self.get_session(session_id) is None:
      raise KeyError(f"Chat session not found: {session_id}")
    with self._uow_factory() as uow:
      records = uow.interactions.list_records("desktop_chat_message", session_id)
    return [DesktopChatMessage.from_dict(record) for record in records]

  async def send_message(self, session_id: str, data: dict[str, Any]) -> dict[str, Any]:
    session = self.get_session(session_id)
    if session is None:
      raise KeyError(f"Chat session not found: {session_id}")
    content = data.get("content")
    if not isinstance(content, str) or not content.strip():
      raise ValueError("content must be a non-empty string.")
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    selected_skill_ids = _string_list(data.get("selected_skill_ids"))
    user_message = DesktopChatMessage(
      message_id=new_id("chat_msg"),
      session_id=session_id,
      role="user",
      content=content.strip(),
      metadata=metadata,
    )
    self._save_message(user_message)
    run_id = str(data.get("run_id") or new_id("chat_run"))
    self._save_session(
      replace(
        session,
        status="running",
        metadata={**session.metadata, "active_run_id": run_id},
        updated_at=utc_now(),
      )
    )
    compaction = self._compact_history_for_session(session_id, session.metadata)
    if self._conversation_task_hub is not None:
      turn = await self._conversation_task_hub.start_daily_turn(
        thread_id=session_id,
        message_id=user_message.message_id,
        content=content.strip(),
        run_id=run_id,
        workspace_id=str(metadata.get("workspace_id") or "default"),
        title=session.title,
        metadata=metadata,
        selected_skill_ids=selected_skill_ids,
        mode=str(data.get("mode") or "agent"),
      )
      launcher_result = {
        **turn.launcher_result,
        "conversation_task": {
          "thread": turn.thread.to_dict(),
          "message": turn.message.to_dict(),
          "objective": turn.objective.to_dict(),
          "task": turn.task.to_dict(),
          "run_id": turn.run_id,
        },
      }
    else:
      launcher_result = await self._launcher.create_task(
        {
          "title": content.strip(),
          "run_id": run_id,
          "input": {
            "chat_session_id": session_id,
            "message_id": user_message.message_id,
            "selected_skill_ids": selected_skill_ids,
            "mode": data.get("mode") or "agent",
          },
        }
      )
    llm_result = await self._complete_with_llm(
      session_id,
      content.strip(),
      selected_skill_ids,
      run_id,
      current_message_id=user_message.message_id,
    )
    assistant_content = str(llm_result.get("content") or "")
    next_status = _session_status_from_llm_result(llm_result)
    pending = _daily_agent_pending(llm_result)
    assistant_message = DesktopChatMessage(
      message_id=new_id("chat_msg"),
      session_id=session_id,
      role="assistant",
      content=assistant_content,
      run_id=launcher_result.get("task", {}).get("run_id") if isinstance(launcher_result.get("task"), dict) else run_id,
      metadata={
        "task_result": launcher_result,
        "llm_result": llm_result,
        "pending": pending,
        "selected_skill_ids": selected_skill_ids,
        "capability_hints": _capability_hints(selected_skill_ids),
      },
    )
    self._save_message(assistant_message)
    title = content.strip()[:42] if session.title == "新的对话" else session.title
    updated = replace(
      session,
      title=title,
      status=next_status,
      message_count=session.message_count + 2,
      metadata={
        **session.metadata,
        "active_run_id": run_id if next_status in {"waiting_for_user", "awaiting_approval"} else None,
        "last_run_id": assistant_message.run_id,
        "last_user_message_id": user_message.message_id,
        "pending": pending,
        **compaction,
      },
      updated_at=utc_now(),
    )
    self._save_session(updated)
    return {
      "session": updated.to_dict(),
      "messages": [user_message.to_dict(), assistant_message.to_dict()],
      "task_result": launcher_result,
    }

  async def retry_last(self, session_id: str) -> dict[str, Any]:
    messages = self.list_messages(session_id)
    for message in reversed(messages):
      if message.role == "user":
        return await self.send_message(
          session_id,
          {
            "content": message.content,
            "metadata": {"retry_of": message.message_id},
          },
        )
    raise ValueError("No user message to retry.")

  def clear_messages(self, session_id: str) -> dict[str, Any]:
    session = self.get_session(session_id)
    if session is None:
      raise KeyError(f"Chat session not found: {session_id}")
    with self._uow_factory() as uow:
      deleted = uow.interactions.delete_records("desktop_chat_message", session_id)
    updated = replace(
      session,
      status="idle",
      message_count=0,
      metadata={**session.metadata, "active_run_id": None, "last_run_id": None, "last_user_message_id": None},
      updated_at=utc_now(),
    )
    self._save_session(updated)
    return {"session": updated.to_dict(), "deleted": deleted}

  def pause(self, session_id: str, reason: str = "user paused chat task") -> dict[str, Any]:
    session = self.get_session(session_id)
    if session is None:
      raise KeyError(f"Chat session not found: {session_id}")
    run_id = session.metadata.get("active_run_id") or session.metadata.get("last_run_id")
    run = None
    if isinstance(run_id, str) and run_id and self._run_control is not None:
      with self._uow_factory() as uow:
        current = uow.states.get(run_id)
      if current is not None and current.status not in {RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.FAILED}:
        run = self._run_control.cancel_run(run_id, reason).to_dict()
    updated = replace(
      session,
      status="paused",
      metadata={**session.metadata, "active_run_id": None, "paused_run_id": run_id},
      updated_at=utc_now(),
    )
    self._save_session(updated)
    return {"session": updated.to_dict(), "run": run}

  async def _complete_with_llm(
    self,
    session_id: str,
    user_content: str,
    selected_skill_ids: list[str],
    run_id: str,
    current_message_id: str | None = None,
  ) -> dict[str, Any]:
    try:
      gateway, provider_name, model_ref = self._resolve_model_gateway()
    except ValueError as exc:
      return {
        "content": (
          "LLM 未配置，无法生成真实大模型回复。\n\n"
          f"原因：{exc}\n\n"
          "请在「配置中心 -> llm」填写 model/base_url/api_key 或 api_key_env，"
          "也可以设置环境变量 AGENT_KERNEL_LLM_MODEL 和 AGENT_KERNEL_LLM_API_KEY。"
        ),
        "error": {"type": "llm_not_configured", "message": str(exc)},
      }
    continuous_result = await self._complete_with_continuous_agent(
      session_id=session_id,
      user_content=user_content,
      selected_skill_ids=selected_skill_ids,
      run_id=run_id,
      gateway=gateway,
      provider_name=provider_name,
      model_ref=model_ref,
      current_message_id=current_message_id,
    )
    if continuous_result is not None:
      return continuous_result
    messages = self._build_llm_messages(
      session_id,
      user_content,
      selected_skill_ids,
      exclude_message_id=current_message_id,
    )
    try:
      result = await gateway.complete(provider_name, model_ref, ModelContext(messages=messages))
    except Exception as exc:
      return {
        "content": f"LLM 调用失败：{exc}",
        "error": {"type": "llm_call_failed", "message": str(exc)},
      }
    content = result.get("content")
    if not isinstance(content, str) or not content.strip():
      return {
        "content": "LLM 返回了空内容。",
        "raw": result,
      }
    return {"content": content, **{key: value for key, value in result.items() if key != "content"}}

  async def _complete_with_continuous_agent(
    self,
    *,
    session_id: str,
    user_content: str,
    selected_skill_ids: list[str],
    run_id: str,
    gateway: ModelGateway,
    provider_name: str,
    model_ref: str,
    current_message_id: str | None = None,
  ) -> dict[str, Any] | None:
    if self._skill_service is None or self._capability_runtime is None or self._atomic_capabilities is None:
      return None
    ensure_builtin_atomic_skills(self._skill_service)
    active_skills = self._skill_service.list_active()
    if selected_skill_ids:
      selected_id_set = set(selected_skill_ids)
      selected = [skill for skill in active_skills if skill.skill_id in selected_id_set]
      active_skills = selected or active_skills
    runner = ContinuousAgentRunner(
      uow_factory=self._uow_factory,
      model_gateway=gateway,
      capability_runtime=self._capability_runtime,
      tool_catalog=self._atomic_capabilities,
      context_assembler=ContextAssembler(
        uow_factory=self._uow_factory,
        memory=self._memory,
      ),
      skills=active_skills,
    )
    try:
      outcome = await runner.run(
        run_id=run_id,
        user_message=user_content,
        history_messages=self._recent_runner_history(session_id, exclude_message_id=current_message_id),
        task_id=session_id,
        agent_id="desktop_daily_agent",
        scope=session_id,
        config=ContinuousRunnerConfig(provider_name=provider_name, model_ref=model_ref, max_turns=8),
      )
    except Exception as exc:
      return {
        "content": f"日常 Agent 执行失败：{exc}",
        "error": {"type": "daily_agent_execution_failed", "message": str(exc)},
      }
    content = _assistant_content_from_runner_result(outcome)
    return {
      "content": content,
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

  def _resolve_model_gateway(self) -> tuple[ModelGateway, str, str]:
    if self._model_gateway is not None and self._model_provider_name and self._model_ref:
      return self._model_gateway, self._model_provider_name, self._model_ref
    config = self._load_llm_config()
    gateway = ModelGateway()
    gateway.register_provider(config.provider, _provider_from_config(config))
    return gateway, config.provider, config.model

  def _load_llm_config(self) -> LLMConfig:
    with self._uow_factory() as uow:
      record = uow.interactions.get_record("config_section", "llm")
    data = record.get("data", {}) if isinstance(record, dict) and isinstance(record.get("data"), dict) else {}
    profile = _llm_profile_from_config(data, agent_id="desktop_daily_agent")
    defaults = self._default_llm_config
    provider = str(
      profile.get("provider")
      or data.get("provider")
      or (defaults.provider if defaults is not None else None)
      or os.getenv("AGENT_KERNEL_LLM_PROVIDER")
      or "openai-compatible"
    )
    model = (
      _string_or_none(profile.get("model"))
      or _string_or_none(data.get("model"))
      or (defaults.model if defaults is not None else None)
      or os.getenv("AGENT_KERNEL_LLM_MODEL")
      or os.getenv("OPENAI_MODEL")
    )
    api_key_env = _string_or_none(profile.get("api_key_env")) or _string_or_none(data.get("api_key_env"))
    api_key = (
      os.getenv(api_key_env) if api_key_env else None
    )
    api_key = (
      api_key
      or _string_or_none(profile.get("api_key"))
      or _string_or_none(data.get("api_key"))
      or (defaults.api_key if defaults is not None else None)
      or os.getenv("AGENT_KERNEL_LLM_API_KEY")
      or os.getenv("OPENAI_API_KEY")
      or os.getenv("GEMINI_API_KEY")
      or os.getenv("ANTHROPIC_API_KEY")
    )
    base_url = (
      _string_or_none(profile.get("base_url"))
      or _string_or_none(data.get("base_url"))
      or (defaults.base_url if defaults is not None else None)
      or os.getenv("AGENT_KERNEL_LLM_BASE_URL")
      or os.getenv("OPENAI_BASE_URL")
      or _default_base_url(provider)
    )
    timeout = data.get(
      "timeout_seconds",
      profile.get(
        "timeout_seconds",
        defaults.timeout_seconds if defaults is not None else os.getenv("AGENT_KERNEL_LLM_TIMEOUT_SECONDS", 60),
      ),
    )
    missing = []
    if not model:
      missing.append("llm.model")
    if not api_key:
      missing.append("llm.api_key 或 llm.api_key_env")
    if missing:
      raise ValueError("缺少 " + "、".join(missing))
    return LLMConfig(
      provider=provider,
      model=model,
      api_key=api_key,
      base_url=str(base_url).rstrip("/"),
      timeout_seconds=float(timeout),
    )

  def _build_llm_messages(
    self,
    session_id: str,
    user_content: str,
    selected_skill_ids: list[str],
    exclude_message_id: str | None = None,
  ) -> list[dict[str, Any]]:
    history = [
      message
      for message in self.list_messages(session_id)[-12:]
      if exclude_message_id is None or message.message_id != exclude_message_id
    ]
    messages: list[dict[str, Any]] = [
      {
        "role": "system",
        "content": (
          "你是 Meadow 桌面端里的日常 Agent。请用中文直接回答。"
          "你可以说明将使用 Skill、Agent、MCP、Workflow 或控制能力；"
          "当前实现会记录任务运行与对话上下文。"
        ),
      }
    ]
    if selected_skill_ids:
      messages.append({"role": "system", "content": f"当前选中的 Skill IDs: {', '.join(selected_skill_ids)}"})
    for message in history:
      if message.role in {"user", "assistant"}:
        messages.append({"role": message.role, "content": message.content})
    messages.append({"role": "user", "content": user_content})
    return messages

  def _recent_runner_history(self, session_id: str, *, exclude_message_id: str | None = None) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    for message in self.list_messages(session_id)[-12:]:
      if exclude_message_id is not None and message.message_id == exclude_message_id:
        continue
      if message.role in {"user", "assistant"} and message.content.strip():
        history.append({"role": message.role, "content": message.content})
    return history

  def _compact_history_for_session(self, session_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
    compacted_ids = _string_list_or_empty(metadata.get("compacted_message_ids"))
    result = self._history_compactor.compact(
      scope=session_id,
      messages=self.list_messages(session_id),
      already_compacted_message_ids=compacted_ids,
      task_id=session_id,
    )
    if result.memory is None:
      return {"compacted_message_ids": compacted_ids}
    merged_ids = [*compacted_ids, *result.compacted_message_ids]
    return {
      "compacted_message_ids": merged_ids,
      "last_compaction_memory_id": result.memory.memory_id,
    }

  def _save_session(self, session: DesktopChatSession) -> None:
    with self._uow_factory() as uow:
      uow.interactions.save_record("desktop_chat_session", session.session_id, session)

  def _save_message(self, message: DesktopChatMessage) -> None:
    with self._uow_factory() as uow:
      uow.interactions.save_record("desktop_chat_message", message.message_id, message, parent_id=message.session_id)


def _string_list(value: object) -> list[str]:
  if value is None:
    return []
  if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
    raise ValueError("selected_skill_ids must be a list of strings.")
  return value


def _string_list_or_empty(value: object) -> list[str]:
  if not isinstance(value, list):
    return []
  return [item for item in value if isinstance(item, str)]


def _capability_hints(selected_skill_ids: list[str]) -> list[dict[str, Any]]:
  hints = [
    {"kind": "workflow", "label": "创建/运行任务", "available": True},
    {"kind": "agent", "label": "子 Agent 委派", "available": True},
    {"kind": "mcp", "label": "MCP 工具", "available": True},
    {"kind": "control", "label": "浏览器/桌面/移动控制", "available": True},
  ]
  if selected_skill_ids:
    hints.insert(0, {"kind": "skill", "label": f"已选择 {len(selected_skill_ids)} 个 Skill", "available": True})
  return hints


def _string_or_none(value: object) -> str | None:
  if value is None:
    return None
  text = str(value).strip()
  return text or None


def _llm_profile_from_config(data: dict[str, Any], *, agent_id: str) -> dict[str, Any]:
  providers = data.get("providers")
  if not isinstance(providers, list) or not providers:
    return {}
  bindings = data.get("agent_bindings")
  binding = _find_mapping(bindings, "agent_id", agent_id) if isinstance(bindings, list) else None
  provider_id = (
    _string_or_none(binding.get("provider_id")) if binding else None
  ) or _string_or_none(data.get("active_provider_id"))
  provider = _find_mapping(providers, "provider_id", provider_id) if provider_id else None
  provider = provider or next((item for item in providers if isinstance(item, dict) and item.get("enabled", True)), None)
  if not isinstance(provider, dict):
    return {}
  models = provider.get("models")
  model_id = (
    _string_or_none(binding.get("model_id")) if binding else None
  ) or _string_or_none(data.get("active_model_id"))
  if not model_id and isinstance(models, list):
    for candidate in models:
      if isinstance(candidate, dict) and candidate.get("enabled", True):
        model_id = _string_or_none(candidate.get("model_id"))
        break
  return {
    "provider": provider.get("kind") or provider.get("provider") or provider.get("platform"),
    "model": model_id,
    "api_key": provider.get("api_key"),
    "api_key_env": provider.get("api_key_env"),
    "base_url": provider.get("base_url"),
    "timeout_seconds": provider.get("timeout_seconds"),
  }


def _find_mapping(items: object, key: str, value: str | None) -> dict[str, Any] | None:
  if not value or not isinstance(items, list):
    return None
  for item in items:
    if isinstance(item, dict) and item.get(key) == value:
      return item
  return None


def _provider_from_config(config: LLMConfig):
  provider_kind = config.provider.lower()
  if provider_kind in {"openai-compatible", "openai", "deepseek", "new-api"}:
    return OpenAICompatibleProvider(
      api_key=config.api_key,
      base_url=config.base_url,
      timeout_seconds=config.timeout_seconds,
    )
  if provider_kind in {"gemini", "google-gemini"}:
    return GeminiProvider(
      api_key=config.api_key,
      base_url=config.base_url,
      timeout_seconds=config.timeout_seconds,
    )
  if provider_kind in {"anthropic", "claude"}:
    return AnthropicMessagesProvider(
      api_key=config.api_key,
      base_url=config.base_url,
      timeout_seconds=config.timeout_seconds,
    )
  raise ValueError(f"暂不支持的模型 provider 类型: {config.provider}")


def _default_base_url(provider: str) -> str:
  provider_kind = provider.lower()
  if provider_kind in {"gemini", "google-gemini"}:
    return "https://generativelanguage.googleapis.com/v1beta"
  if provider_kind in {"anthropic", "claude"}:
    return "https://api.anthropic.com/v1"
  return "https://api.openai.com/v1"


def _assistant_content_from_runner_output(output: dict[str, Any]) -> str:
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


def _assistant_content_from_runner_result(outcome: ContinuousRunnerResult) -> str:
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
  return _assistant_content_from_runner_output(outcome.output)


def _daily_agent_pending(llm_result: dict[str, Any]) -> dict[str, Any] | None:
  daily_agent = llm_result.get("daily_agent")
  if not isinstance(daily_agent, dict):
    return None
  pending = daily_agent.get("pending")
  return pending if isinstance(pending, dict) else None


def _session_status_from_llm_result(llm_result: dict[str, Any]) -> str:
  daily_agent = llm_result.get("daily_agent")
  if not isinstance(daily_agent, dict):
    return "idle"
  status = daily_agent.get("status")
  if status in {"waiting_for_user", "awaiting_approval"}:
    return str(status)
  return "idle"
