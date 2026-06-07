"""User-facing scheduled task service."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any, Protocol

from agent_kernel.domain.base import new_id, utc_now
from agent_kernel.domain.scheduled_task import ScheduledTask, ScheduledTaskTrigger


class ScheduledTaskLauncher(Protocol):
  async def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
    ...


class ScheduledTaskService:
  def __init__(self, uow_factory, launcher: ScheduledTaskLauncher) -> None:
    self._uow_factory = uow_factory
    self._launcher = launcher

  def create(self, data: dict[str, Any]) -> ScheduledTask:
    task = ScheduledTask(
      task_id=str(data["task_id"]) if data.get("task_id") else new_id("scheduled_task"),
      name=str(data.get("name") or data.get("title") or "scheduled task"),
      schedule_kind=str(data.get("schedule_kind") or "at"),
      schedule_value=str(data.get("schedule_value") or ""),
      payload=data.get("payload") if isinstance(data.get("payload"), dict) else {},
      enabled=bool(data.get("enabled", True)),
      max_triggers=int(data["max_triggers"]) if data.get("max_triggers") is not None else None,
      metadata=data.get("metadata") if isinstance(data.get("metadata"), dict) else {},
    )
    with self._uow_factory() as uow:
      uow.interactions.save_scheduled_task(task)
    return task

  def list(self, *, enabled_only: bool = False) -> list[ScheduledTask]:
    with self._uow_factory() as uow:
      tasks = uow.interactions.list_scheduled_tasks()
    return [task for task in tasks if task.enabled] if enabled_only else tasks

  def get(self, task_id: str) -> ScheduledTask | None:
    with self._uow_factory() as uow:
      return uow.interactions.get_scheduled_task(task_id)

  def update_state(self, task_id: str, *, enabled: bool) -> ScheduledTask:
    with self._uow_factory() as uow:
      task = uow.interactions.get_scheduled_task(task_id)
      if task is None:
        raise KeyError(f"Scheduled task not found: {task_id}")
      updated = replace(task, enabled=enabled, updated_at=utc_now())
      uow.interactions.save_scheduled_task(updated)
      return updated

  async def run_due(self, now: datetime | None = None, limit: int | None = None) -> list[ScheduledTaskTrigger]:
    current = now or utc_now()
    due = [task for task in self.list(enabled_only=True) if task.next_run_at is not None and task.next_run_at <= current]
    if limit is not None:
      due = due[:limit]
    triggers: list[ScheduledTaskTrigger] = []
    for task in due:
      result = await self._launcher.create_task(task.payload)
      task.mark_triggered(current)
      trigger = ScheduledTaskTrigger(scheduled_task_id=task.task_id, result=result)
      with self._uow_factory() as uow:
        uow.interactions.save_scheduled_task(task)
        uow.interactions.save_scheduled_task_trigger(trigger)
      triggers.append(trigger)
    return triggers

  def list_triggers(self, task_id: str) -> list[ScheduledTaskTrigger]:
    with self._uow_factory() as uow:
      return uow.interactions.list_scheduled_task_triggers(task_id)
