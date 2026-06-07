"""TaskBoard helpers."""

from dataclasses import replace

from agent_kernel.domain.base import new_id, utc_now
from agent_kernel.domain.interaction import TaskBoardItem


class TaskBoardService:
  def __init__(self, uow_factory) -> None:
    self._uow_factory = uow_factory

  def create_item(self, title: str, assignee_pool_id: str | None = None) -> TaskBoardItem:
    item = TaskBoardItem(
      item_id=new_id("taskboard_item"),
      title=title,
      assignee_pool_id=assignee_pool_id,
    )
    with self._uow_factory() as uow:
      uow.interactions.save_taskboard_item(item)
    return item

  def assign(self, item: TaskBoardItem, session_id: str) -> TaskBoardItem:
    assigned = replace(
      item,
      assignee_session_id=session_id,
      status="doing",
      updated_at=utc_now(),
    )
    with self._uow_factory() as uow:
      uow.interactions.save_taskboard_item(assigned)
    return assigned

