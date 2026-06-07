"""Conversation-to-task coordination for host-facing daily Agent entrypoints."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from agent_kernel.domain.base import DomainModel, new_id, utc_now
from agent_kernel.domain.conversation import Message, MessageRole, Objective, Thread
from agent_kernel.domain.identifiers import EntityRef
from agent_kernel.domain.states import TaskStatus
from agent_kernel.domain.task import Task


class ConversationTaskLauncher(Protocol):
  async def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
    ...


@dataclass(slots=True)
class DailyAgentTurn(DomainModel):
  thread: Thread
  message: Message
  objective: Objective
  task: Task
  run_id: str
  launcher_result: dict[str, Any]


class ConversationTaskHub:
  """Maps daily chat turns to durable conversation/task/run records.

  The hub is an application-layer coordinator. It does not call models,
  execute tools, or decide which Skill/capability to use.
  """

  def __init__(self, uow_factory, task_launcher: ConversationTaskLauncher) -> None:
    self._uow_factory = uow_factory
    self._task_launcher = task_launcher

  async def start_daily_turn(
    self,
    *,
    thread_id: str,
    message_id: str,
    content: str,
    run_id: str,
    workspace_id: str = "default",
    title: str | None = None,
    metadata: dict[str, Any] | None = None,
    selected_skill_ids: list[str] | None = None,
    mode: str = "agent",
  ) -> DailyAgentTurn:
    if not content.strip():
      raise ValueError("content must be a non-empty string.")
    thread = self._upsert_thread(thread_id, workspace_id=workspace_id, title=title or content.strip()[:42])
    message = Message(
      message_id=message_id,
      role=MessageRole.USER,
      content={
        "text": content.strip(),
        "metadata": metadata or {},
        "selected_skill_ids": selected_skill_ids or [],
        "mode": mode,
      },
    )
    objective = Objective(
      objective_id=new_id("objective"),
      thread_id=thread.thread_id,
      description=content.strip(),
    )
    task = Task(
      task_id=new_id("task"),
      objective_id=objective.objective_id,
      title=content.strip()[:120],
      status=TaskStatus.READY,
    )
    updated_thread = Thread(
      thread_id=thread.thread_id,
      workspace_id=thread.workspace_id,
      title=thread.title or title or content.strip()[:42],
      message_refs=[*thread.message_refs, EntityRef(id=message.message_id, kind="message")],
      created_at=thread.created_at,
      updated_at=utc_now(),
    )
    with self._uow_factory() as uow:
      thread_record_id = _record_id("conversation_thread", updated_thread.thread_id)
      objective_record_id = _record_id("objective", objective.objective_id)
      uow.interactions.save_record("conversation_thread", thread_record_id, updated_thread)
      uow.interactions.save_record(
        "conversation_message",
        _record_id("conversation_message", message.message_id),
        message,
        parent_id=thread_record_id,
      )
      uow.interactions.save_record("objective", objective_record_id, objective, parent_id=thread_record_id)
      uow.interactions.save_record("task", _record_id("task", task.task_id), task, parent_id=objective_record_id)
      uow.interactions.save_record(
        "thread_task_link",
        _record_id("thread_task_link", f"{thread.thread_id}_{task.task_id}"),
        {
          "thread_id": thread.thread_id,
          "message_id": message.message_id,
          "objective_id": objective.objective_id,
          "task_id": task.task_id,
          "run_id": run_id,
        },
        parent_id=thread_record_id,
      )
    launcher_result = await self._task_launcher.create_task(
      {
        "title": content.strip(),
        "run_id": run_id,
        "input": {
          "thread_id": thread.thread_id,
          "message_id": message.message_id,
          "objective_id": objective.objective_id,
          "task_id": task.task_id,
          "selected_skill_ids": selected_skill_ids or [],
          "mode": mode,
        },
      }
    )
    return DailyAgentTurn(
      thread=updated_thread,
      message=message,
      objective=objective,
      task=task,
      run_id=run_id,
      launcher_result=launcher_result,
    )

  def _upsert_thread(self, thread_id: str, *, workspace_id: str, title: str) -> Thread:
    with self._uow_factory() as uow:
      record = uow.interactions.get_record("conversation_thread", _record_id("conversation_thread", thread_id))
    if record is not None:
      return Thread.from_dict(record)
    return Thread(thread_id=thread_id, workspace_id=workspace_id, title=title)


def _record_id(record_type: str, domain_id: str) -> str:
  return f"{record_type}:{domain_id}"
