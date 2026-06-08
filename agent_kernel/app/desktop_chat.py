"""Desktop chat session service."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Protocol

from agent_kernel.app.conversation_task_hub import ConversationTaskHub
from agent_kernel.app.daily_agent import (
  DAILY_AGENT_ID,
  ContinuousDailyAgentExecutor,
  DailyAgentExecutor,
  DailyAgentRequest,
)
from agent_kernel.app.model_binding import (
  ConfigModelBindingProvider,
  ModelBindingProvider,
  StaticModelBindingProvider,
)
from agent_kernel.autonomy.skill_service import SkillService
from agent_kernel.capabilities.atomic import AtomicCapabilityProvider
from agent_kernel.capabilities.runtime import CapabilityRuntime
from agent_kernel.config import LLMConfig
from agent_kernel.app.conversation_history import ConversationHistoryCompactor
from agent_kernel.domain.base import DomainModel, new_id, utc_now
from agent_kernel.domain.context import ModelContext
from agent_kernel.domain.states import RunStatus
from agent_kernel.memory import MemoryFacade
from agent_kernel.models import ModelGateway


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
    model_binding_provider: ModelBindingProvider | None = None,
    daily_agent_executor: DailyAgentExecutor | None = None,
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
    if model_binding_provider is not None:
      self._model_binding_provider = model_binding_provider
    elif model_gateway is not None and model_provider_name and model_ref:
      self._model_binding_provider = StaticModelBindingProvider(model_gateway, model_provider_name, model_ref)
    else:
      self._model_binding_provider = ConfigModelBindingProvider(uow_factory, default_llm_config)
    self._daily_agent_executor = daily_agent_executor
    if self._daily_agent_executor is None and skill_service is not None and capability_runtime is not None and atomic_capabilities is not None:
      self._daily_agent_executor = ContinuousDailyAgentExecutor(
        uow_factory=uow_factory,
        skill_service=skill_service,
        capability_runtime=capability_runtime,
        tool_catalog=atomic_capabilities,
        memory=self._memory,
      )

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
      binding = self._model_binding_provider.resolve(DAILY_AGENT_ID)
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
    if self._daily_agent_executor is not None:
      try:
        response = await self._daily_agent_executor.execute(
          DailyAgentRequest(
            session_id=session_id,
            run_id=run_id,
            user_content=user_content,
            history_messages=self._recent_runner_history(session_id, exclude_message_id=current_message_id),
            selected_skill_ids=selected_skill_ids,
            provider_name=binding.provider_name,
            model_ref=binding.model_ref,
            agent_id=DAILY_AGENT_ID,
          ),
          binding.gateway,
        )
      except Exception as exc:
        return {
          "content": f"日常 Agent 执行失败：{exc}",
          "error": {"type": "daily_agent_execution_failed", "message": str(exc)},
        }
      return {"content": response.content, **response.raw}
    messages = self._build_llm_messages(
      session_id,
      user_content,
      selected_skill_ids,
      exclude_message_id=current_message_id,
    )
    try:
      result = await binding.gateway.complete(binding.provider_name, binding.model_ref, ModelContext(messages=messages))
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

  def _load_llm_config(self) -> LLMConfig:
    if isinstance(self._model_binding_provider, ConfigModelBindingProvider):
      return self._model_binding_provider.load_config(DAILY_AGENT_ID)
    binding = self._model_binding_provider.resolve(DAILY_AGENT_ID)
    if binding.config is None:
      raise RuntimeError("Current model binding does not expose an LLMConfig.")
    return binding.config

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
