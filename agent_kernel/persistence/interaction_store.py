"""Interaction fabric persistence."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from agent_kernel.domain.interaction import (
  AgentPool,
  DiscussionTurn,
  GroupChatSession,
  HandoffRecord,
  InteractionChannel,
  InteractionMessage,
  InteractionParticipant,
  MergeQueueItem,
  ObservationFinding,
  PatchArtifact,
  ReviewRecord,
  TaskBoardItem,
  WorkspaceLease,
)
from agent_kernel.domain.delegation import DelegationTask
from agent_kernel.domain.mcp_config import MCPServerDefinition
from agent_kernel.domain.scheduled_task import ScheduledTask, ScheduledTaskTrigger
from agent_kernel.domain.serialization import to_primitive
from agent_kernel.domain.workbench import CollaborationWorkbench, WorkbenchMember, WorkbenchTaskSlice


class InteractionStore:
  def __init__(self, conn: sqlite3.Connection) -> None:
    self._conn = conn

  def save_participant(self, item: InteractionParticipant) -> None:
    self._save(item.participant_id, "participant", None, item)

  def save_channel(self, item: InteractionChannel) -> None:
    self._save(item.channel_id, "channel", None, item)

  def get_channel(self, channel_id: str) -> InteractionChannel | None:
    data = self._get(channel_id)
    return InteractionChannel.from_dict(data) if data is not None else None

  def list_channels(self) -> list[InteractionChannel]:
    return [InteractionChannel.from_dict(data) for data in self._list_all("channel")]

  def save_message(self, item: InteractionMessage) -> None:
    self._save(item.message_id, "message", item.channel_id, item)

  def list_messages(self, channel_id: str) -> list[InteractionMessage]:
    return [InteractionMessage.from_dict(data) for data in self._list("message", channel_id)]

  def save_group_chat(self, item: GroupChatSession) -> None:
    self._save(item.group_chat_id, "group_chat", item.thread_id, item)

  def get_group_chat(self, group_chat_id: str) -> GroupChatSession | None:
    data = self._get(group_chat_id)
    return GroupChatSession.from_dict(data) if data is not None else None

  def save_turn(self, item: DiscussionTurn) -> None:
    self._save(item.turn_id, "discussion_turn", item.group_chat_id, item)

  def list_turns(self, group_chat_id: str) -> list[DiscussionTurn]:
    return [DiscussionTurn.from_dict(data) for data in self._list("discussion_turn", group_chat_id)]

  def save_agent_pool(self, item: AgentPool) -> None:
    self._save(item.pool_id, "agent_pool", None, item)

  def get_agent_pool(self, pool_id: str) -> AgentPool | None:
    data = self._get(pool_id)
    return AgentPool.from_dict(data) if data is not None else None

  def save_taskboard_item(self, item: TaskBoardItem) -> None:
    self._save(item.item_id, "taskboard_item", item.assignee_pool_id, item)

  def list_taskboard_items(self) -> list[TaskBoardItem]:
    return [TaskBoardItem.from_dict(data) for data in self._list_all("taskboard_item")]

  def save_finding(self, item: ObservationFinding) -> None:
    self._save(item.finding_id, "observation_finding", item.target_run_id, item)

  def list_findings(self, target_run_id: str) -> list[ObservationFinding]:
    return [ObservationFinding.from_dict(data) for data in self._list("observation_finding", target_run_id)]

  def save_workspace_lease(self, item: WorkspaceLease) -> None:
    self._save(item.lease_id, "workspace_lease", item.task_id, item)

  def get_workspace_lease(self, lease_id: str) -> WorkspaceLease | None:
    data = self._get(lease_id)
    return WorkspaceLease.from_dict(data) if data is not None else None

  def list_workspace_leases(self, task_id: str) -> list[WorkspaceLease]:
    return [WorkspaceLease.from_dict(data) for data in self._list("workspace_lease", task_id)]

  def save_patch_artifact(self, item: PatchArtifact) -> None:
    self._save(item.patch_id, "patch_artifact", item.lease_id, item)

  def get_patch_artifact(self, patch_id: str) -> PatchArtifact | None:
    data = self._get(patch_id)
    return PatchArtifact.from_dict(data) if data is not None else None

  def list_patch_artifacts(self, lease_id: str) -> list[PatchArtifact]:
    return [PatchArtifact.from_dict(data) for data in self._list("patch_artifact", lease_id)]

  def save_review_record(self, item: ReviewRecord) -> None:
    self._save(item.review_id, "review_record", item.patch_id, item)

  def list_review_records(self, patch_id: str) -> list[ReviewRecord]:
    return [ReviewRecord.from_dict(data) for data in self._list("review_record", patch_id)]

  def save_merge_queue_item(self, item: MergeQueueItem) -> None:
    self._save(item.queue_item_id, "merge_queue_item", item.task_id, item)

  def get_merge_queue_item(self, queue_item_id: str) -> MergeQueueItem | None:
    data = self._get(queue_item_id)
    return MergeQueueItem.from_dict(data) if data is not None else None

  def list_merge_queue_items(self, task_id: str | None = None) -> list[MergeQueueItem]:
    records = self._list("merge_queue_item", task_id) if task_id is not None else self._list_all("merge_queue_item")
    return [MergeQueueItem.from_dict(data) for data in records]

  def save_handoff(self, item: HandoffRecord) -> None:
    self._save(item.handoff_id, "handoff", item.task_id, item)

  def get_handoff(self, handoff_id: str) -> HandoffRecord | None:
    data = self._get(handoff_id)
    return HandoffRecord.from_dict(data) if data is not None else None

  def list_handoffs(self, task_id: str) -> list[HandoffRecord]:
    return [HandoffRecord.from_dict(data) for data in self._list("handoff", task_id)]

  def save_workbench(self, item: CollaborationWorkbench) -> None:
    self._save(item.workbench_id, "collaboration_workbench", None, item)

  def get_workbench(self, workbench_id: str) -> CollaborationWorkbench | None:
    data = self._get(workbench_id)
    return CollaborationWorkbench.from_dict(data) if data is not None else None

  def list_workbenches(self) -> list[CollaborationWorkbench]:
    return [CollaborationWorkbench.from_dict(data) for data in self._list_all("collaboration_workbench")]

  def save_workbench_member(self, workbench_id: str, item: WorkbenchMember) -> None:
    self._save(item.member_id, "workbench_member", workbench_id, item)

  def list_workbench_members(self, workbench_id: str) -> list[WorkbenchMember]:
    return [WorkbenchMember.from_dict(data) for data in self._list("workbench_member", workbench_id)]

  def save_workbench_task_slice(self, workbench_id: str, item: WorkbenchTaskSlice) -> None:
    self._save(item.slice_id, "workbench_task_slice", workbench_id, item)

  def list_workbench_task_slices(self, workbench_id: str) -> list[WorkbenchTaskSlice]:
    return [WorkbenchTaskSlice.from_dict(data) for data in self._list("workbench_task_slice", workbench_id)]

  def save_delegation_task(self, item: DelegationTask) -> None:
    self._save(item.task_id, "delegation_task", item.parent_run_id, item)

  def get_delegation_task(self, task_id: str) -> DelegationTask | None:
    data = self._get(task_id)
    return DelegationTask.from_dict(data) if data is not None else None

  def list_delegation_tasks_by_parent(self, parent_run_id: str) -> list[DelegationTask]:
    return [DelegationTask.from_dict(data) for data in self._list("delegation_task", parent_run_id)]

  def list_delegation_tasks(self) -> list[DelegationTask]:
    return [DelegationTask.from_dict(data) for data in self._list_all("delegation_task")]

  def save_mcp_server(self, item: MCPServerDefinition) -> None:
    self._save(item.name, "mcp_server", None, item)

  def get_mcp_server(self, name: str) -> MCPServerDefinition | None:
    data = self._get(name)
    return MCPServerDefinition.from_dict(data) if data is not None else None

  def list_mcp_servers(self) -> list[MCPServerDefinition]:
    return [MCPServerDefinition.from_dict(data) for data in self._list_all("mcp_server")]

  def delete_mcp_server(self, name: str) -> bool:
    cursor = self._conn.execute(
      "DELETE FROM interaction_records WHERE record_type = ? AND record_id = ?",
      ("mcp_server", name),
    )
    return cursor.rowcount > 0

  def save_scheduled_task(self, item: ScheduledTask) -> None:
    self._save(item.task_id, "scheduled_task", None, item)

  def get_scheduled_task(self, task_id: str) -> ScheduledTask | None:
    data = self._get(task_id)
    return ScheduledTask.from_dict(data) if data is not None else None

  def list_scheduled_tasks(self) -> list[ScheduledTask]:
    return [ScheduledTask.from_dict(data) for data in self._list_all("scheduled_task")]

  def save_scheduled_task_trigger(self, item: ScheduledTaskTrigger) -> None:
    self._save(item.trigger_id, "scheduled_task_trigger", item.scheduled_task_id, item)

  def list_scheduled_task_triggers(self, scheduled_task_id: str) -> list[ScheduledTaskTrigger]:
    return [ScheduledTaskTrigger.from_dict(data) for data in self._list("scheduled_task_trigger", scheduled_task_id)]

  def delete_scheduled_task(self, task_id: str) -> bool:
    cursor = self._conn.execute(
      "DELETE FROM interaction_records WHERE record_type = ? AND record_id = ?",
      ("scheduled_task", task_id),
    )
    return cursor.rowcount > 0

  def save_record(self, record_type: str, record_id: str, value: Any, parent_id: str | None = None) -> None:
    self._save(record_id, record_type, parent_id, value)

  def get_record(self, record_type: str, record_id: str) -> dict[str, Any] | None:
    row = self._conn.execute(
      """
      SELECT record_json
      FROM interaction_records
      WHERE record_type = ?
        AND record_id = ?
      """,
      (record_type, record_id),
    ).fetchone()
    if row is None:
      return None
    return json.loads(row["record_json"])

  def list_records(self, record_type: str, parent_id: str | None = None) -> list[dict[str, Any]]:
    return self._list_all(record_type) if parent_id is None else self._list(record_type, parent_id)

  def delete_record(self, record_type: str, record_id: str) -> bool:
    cursor = self._conn.execute(
      "DELETE FROM interaction_records WHERE record_type = ? AND record_id = ?",
      (record_type, record_id),
    )
    return cursor.rowcount > 0

  def delete_records(self, record_type: str, parent_id: str) -> int:
    cursor = self._conn.execute(
      "DELETE FROM interaction_records WHERE record_type = ? AND parent_id = ?",
      (record_type, parent_id),
    )
    return cursor.rowcount

  def _save(self, record_id: str, record_type: str, parent_id: str | None, value: Any) -> None:
    self._conn.execute(
      """
      INSERT INTO interaction_records (record_id, record_type, parent_id, record_json, created_at)
      VALUES (?, ?, ?, ?, ?)
      ON CONFLICT(record_id) DO UPDATE SET
        record_json = excluded.record_json
      """,
      (
        record_id,
        record_type,
        parent_id,
        json.dumps(to_primitive(value), ensure_ascii=False, sort_keys=True),
        value.created_at.isoformat() if hasattr(value, "created_at") else "",
      ),
    )

  def _get(self, record_id: str) -> dict[str, Any] | None:
    row = self._conn.execute(
      "SELECT record_json FROM interaction_records WHERE record_id = ?",
      (record_id,),
    ).fetchone()
    if row is None:
      return None
    return json.loads(row["record_json"])

  def _list(self, record_type: str, parent_id: str) -> list[dict[str, Any]]:
    rows = self._conn.execute(
      """
      SELECT record_json
      FROM interaction_records
      WHERE record_type = ?
        AND parent_id = ?
      ORDER BY created_at ASC, record_id ASC
      """,
      (record_type, parent_id),
    ).fetchall()
    return [json.loads(row["record_json"]) for row in rows]

  def _list_all(self, record_type: str) -> list[dict[str, Any]]:
    rows = self._conn.execute(
      """
      SELECT record_json
      FROM interaction_records
      WHERE record_type = ?
      ORDER BY created_at ASC, record_id ASC
      """,
      (record_type,),
    ).fetchall()
    return [json.loads(row["record_json"]) for row in rows]
