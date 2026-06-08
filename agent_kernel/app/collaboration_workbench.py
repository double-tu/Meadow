"""Application service for multi-agent collaboration workbenches."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Protocol

from agent_kernel.agents.group_chat import DecisionArtifactService, GroupChatService
from agent_kernel.agents.interaction_fabric import InteractionFabric
from agent_kernel.agents.taskboard import TaskBoardService
from agent_kernel.domain.base import DomainModel, new_id, utc_now
from agent_kernel.domain.delegation import DelegationTaskReport
from agent_kernel.domain.events import RuntimeEvent, RuntimeEventType
from agent_kernel.domain.interaction import (
  ChannelMode,
  InteractionChannel,
  InteractionMessage,
  InteractionParticipant,
  ParticipantKind,
  SpeakerPolicy,
  SpeakerPolicyType,
)
from agent_kernel.domain.workbench import (
  CollaborationWorkbench,
  CollaborationWorkbenchKind,
  CollaborationWorkbenchStatus,
  WorkbenchMember,
  WorkbenchTaskSlice,
  WorkbenchTaskSliceStatus,
)


class WorkbenchDelegationControl(Protocol):
  async def delegate(
    self,
    *,
    parent_run_id: str,
    task: str,
    connector_id: str,
    agent_type: str | None = None,
    parent_agent_id: str | None = None,
    parent_session_id: str | None = None,
    metadata: dict[str, Any] | None = None,
  ) -> DelegationTaskReport:
    ...

  async def cancel(
    self,
    *,
    parent_run_id: str,
    task_id: str,
    reason: str = "delegation cancelled",
  ) -> DelegationTaskReport:
    ...


@dataclass(slots=True)
class WorkbenchSnapshot(DomainModel):
  workbench: CollaborationWorkbench
  channel: InteractionChannel | None = None
  group_chat: dict[str, Any] | None = None
  members: list[WorkbenchMember] = None  # type: ignore[assignment]
  task_slices: list[WorkbenchTaskSlice] = None  # type: ignore[assignment]
  messages: list[InteractionMessage] = None  # type: ignore[assignment]
  delegations: list[dict[str, Any]] = None  # type: ignore[assignment]
  taskboard_items: list[dict[str, Any]] = None  # type: ignore[assignment]

  def __post_init__(self) -> None:
    if self.members is None:
      self.members = []
    if self.task_slices is None:
      self.task_slices = []
    if self.messages is None:
      self.messages = []
    if self.delegations is None:
      self.delegations = []
    if self.taskboard_items is None:
      self.taskboard_items = []


class CollaborationWorkbenchService:
  """Composes channels, task slices, group chat, and delegation into one workspace."""

  def __init__(
    self,
    uow_factory,
    *,
    delegation_control: WorkbenchDelegationControl | None = None,
    fabric: InteractionFabric | None = None,
    group_chat_service: GroupChatService | None = None,
    taskboard: TaskBoardService | None = None,
  ) -> None:
    self._uow_factory = uow_factory
    self._delegation_control = delegation_control
    self._fabric = fabric or InteractionFabric(uow_factory)
    self._group_chat = group_chat_service or GroupChatService(uow_factory, self._fabric)
    self._taskboard = taskboard or TaskBoardService(uow_factory)

  def list(self) -> list[WorkbenchSnapshot]:
    with self._uow_factory() as uow:
      workbenches = uow.interactions.list_workbenches()
    return [self.snapshot(workbench.workbench_id) for workbench in workbenches]

  def snapshot(self, workbench_id: str) -> WorkbenchSnapshot:
    with self._uow_factory() as uow:
      workbench = uow.interactions.get_workbench(workbench_id)
      if workbench is None:
        raise KeyError(f"Collaboration workbench not found: {workbench_id}")
      channel = uow.interactions.get_channel(workbench.channel_id) if workbench.channel_id else None
      members = uow.interactions.list_workbench_members(workbench_id)
      slices = uow.interactions.list_workbench_task_slices(workbench_id)
      messages = uow.interactions.list_messages(channel.channel_id) if channel else []
      delegations = [
        task.to_dict()
        for task in uow.interactions.list_delegation_tasks_by_parent(workbench.parent_run_id or "")
      ]
      taskboard_items = [
        item.to_dict()
        for item in uow.interactions.list_taskboard_items()
        if item.item_id in set(workbench.taskboard_item_ids)
      ]
      group_chat = (
        uow.interactions.get_group_chat(workbench.group_chat_id).to_dict()
        if workbench.group_chat_id and uow.interactions.get_group_chat(workbench.group_chat_id)
        else None
      )
    return WorkbenchSnapshot(
      workbench=workbench,
      channel=channel,
      group_chat=group_chat,
      members=members,
      task_slices=slices,
      messages=messages,
      delegations=delegations,
      taskboard_items=taskboard_items,
    )

  def create_group_chat(self, payload: dict[str, Any]) -> WorkbenchSnapshot:
    workbench, channel, members = self._create_base_workbench(
      payload,
      kind=CollaborationWorkbenchKind.GROUP_CHAT,
      default_title="群聊工作台",
      channel_mode=ChannelMode.ROUND_ROBIN,
    )
    policy = _speaker_policy(payload.get("speaker_policy"))
    group_chat = self._group_chat.create(
      thread_id=workbench.workbench_id,
      topic=workbench.objective,
      participant_ids=channel.participant_ids,
      turn_limit=_positive_int(payload.get("turn_limit"), default=10, field="turn_limit"),
      speaker_policy=policy,
      moderator_participant_id=_optional_str(payload.get("moderator_participant_id")),
    )
    workbench = replace(
      workbench,
      group_chat_id=group_chat.group_chat_id,
      status=CollaborationWorkbenchStatus.RUNNING,
      updated_at=utc_now(),
    )
    self._save_workbench(workbench, members, [])
    self._append_event(RuntimeEventType.WORKBENCH_CREATED, workbench, {"channel_id": channel.channel_id})
    return self.snapshot(workbench.workbench_id)

  async def create_cli_collaboration(self, payload: dict[str, Any]) -> WorkbenchSnapshot:
    workbench, channel, members = self._create_base_workbench(
      payload,
      kind=CollaborationWorkbenchKind.CLI_COLLABORATION,
      default_title="CLI 协同工作台",
      channel_mode=ChannelMode.AD_HOC,
    )
    slices = self._build_task_slices(payload, workbench, members, default_slice_prefix="CLI 协作")
    workbench, slices = await self._maybe_start_delegations(payload, workbench, members, slices)
    workbench = replace(workbench, status=CollaborationWorkbenchStatus.RUNNING, updated_at=utc_now())
    self._save_workbench(workbench, members, slices)
    self._append_event(
      RuntimeEventType.WORKBENCH_CREATED,
      workbench,
      {"channel_id": channel.channel_id, "slice_count": len(slices)},
    )
    return self.snapshot(workbench.workbench_id)

  async def create_parallel_delegation(self, payload: dict[str, Any]) -> WorkbenchSnapshot:
    workbench, channel, members = self._create_base_workbench(
      payload,
      kind=CollaborationWorkbenchKind.PARALLEL_DELEGATION,
      default_title="并行子 Agent 工作台",
      channel_mode=ChannelMode.BROADCAST,
    )
    slices = self._build_task_slices(payload, workbench, members, default_slice_prefix="并行任务")
    workbench, slices = await self._maybe_start_delegations(payload, workbench, members, slices)
    workbench = replace(workbench, status=CollaborationWorkbenchStatus.RUNNING, updated_at=utc_now())
    self._save_workbench(workbench, members, slices)
    self._append_event(
      RuntimeEventType.WORKBENCH_CREATED,
      workbench,
      {"channel_id": channel.channel_id, "slice_count": len(slices), "parallel": True},
    )
    return self.snapshot(workbench.workbench_id)

  async def create_technical_review(self, payload: dict[str, Any]) -> WorkbenchSnapshot:
    workbench, channel, members = self._create_base_workbench(
      payload,
      kind=CollaborationWorkbenchKind.TECHNICAL_REVIEW,
      default_title="技术评审工作台",
      channel_mode=ChannelMode.REVIEW_QUEUE,
    )
    slices = self._build_review_slices(payload, workbench, members)
    workbench, slices = await self._maybe_start_delegations(payload, workbench, members, slices)
    workbench = replace(
      workbench,
      status=CollaborationWorkbenchStatus.RUNNING,
      metadata={
        **workbench.metadata,
        "target_ref": _optional_str(payload.get("target_ref")),
        "review_policy": payload.get("review_policy") if isinstance(payload.get("review_policy"), dict) else {},
      },
      updated_at=utc_now(),
    )
    self._save_workbench(workbench, members, slices)
    self._append_event(
      RuntimeEventType.WORKBENCH_CREATED,
      workbench,
      {"channel_id": channel.channel_id, "slice_count": len(slices), "review": True},
    )
    return self.snapshot(workbench.workbench_id)

  def send_message(self, workbench_id: str, payload: dict[str, Any]) -> InteractionMessage:
    snapshot = self.snapshot(workbench_id)
    if snapshot.channel is None:
      raise ValueError(f"Workbench has no channel: {workbench_id}")
    sender = _required_str(payload.get("sender_participant_id"), "sender_participant_id")
    allowed = {member.participant_id for member in snapshot.members}
    if sender not in allowed:
      raise ValueError(f"sender_participant_id is not a workbench member: {sender}")
    content = payload.get("content")
    if isinstance(content, dict):
      message_content = content
    else:
      message_content = {"type": "chat", "text": _required_str(payload.get("text"), "text")}
    return self._fabric.send_message(snapshot.channel.channel_id, sender, message_content)

  def create_decision_artifact(self, workbench_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    snapshot = self.snapshot(workbench_id)
    if snapshot.channel is None or snapshot.workbench.group_chat_id is None:
      raise ValueError("Decision artifacts require a group chat workbench.")
    with self._uow_factory() as uow:
      group_chat = uow.interactions.get_group_chat(snapshot.workbench.group_chat_id)
    if group_chat is None:
      raise KeyError(f"Group chat not found: {snapshot.workbench.group_chat_id}")
    artifact = DecisionArtifactService(self._uow_factory, self._fabric).create_for_group_chat(
      group_chat,
      snapshot.channel.channel_id,
      decided_by_participant_id=_optional_str((payload or {}).get("decided_by_participant_id")),
    )
    return {"ok": True, "artifact": artifact.to_dict(), "workbench": self.snapshot(workbench_id).workbench.to_dict()}

  async def cancel(self, workbench_id: str, reason: str = "workbench cancelled") -> WorkbenchSnapshot:
    snapshot = self.snapshot(workbench_id)
    workbench = replace(
      snapshot.workbench,
      status=CollaborationWorkbenchStatus.CANCELLED,
      updated_at=utc_now(),
    )
    if self._delegation_control is not None and workbench.parent_run_id:
      for task_id in workbench.delegation_task_ids:
        await self._delegation_control.cancel(
          parent_run_id=workbench.parent_run_id,
          task_id=task_id,
          reason=reason,
        )
    with self._uow_factory() as uow:
      uow.interactions.save_workbench(workbench)
    self._append_event(RuntimeEventType.WORKBENCH_CANCELLED, workbench, {"reason": reason})
    return self.snapshot(workbench_id)

  def _create_base_workbench(
    self,
    payload: dict[str, Any],
    *,
    kind: CollaborationWorkbenchKind,
    default_title: str,
    channel_mode: ChannelMode,
  ) -> tuple[CollaborationWorkbench, InteractionChannel, list[WorkbenchMember]]:
    workbench_id = new_id("workbench")
    title = _optional_str(payload.get("title")) or _optional_str(payload.get("topic")) or default_title
    objective = (
      _optional_str(payload.get("objective"))
      or _optional_str(payload.get("task"))
      or _optional_str(payload.get("topic"))
      or title
    )
    parent_run_id = _optional_str(payload.get("parent_run_id")) or f"workbench:{workbench_id}"
    members = self._build_members(payload.get("members"), workbench_id)
    for member in members:
      self._fabric.add_participant(
        InteractionParticipant(
          participant_id=member.participant_id,
          kind=member.kind,
          role=member.role,
          agent_session_id=member.agent_session_id,
          permissions=_permissions_for_member(member),
        )
      )
    channel = self._fabric.create_channel(
      title,
      [member.participant_id for member in members],
      mode=channel_mode,
      bound_run_id=parent_run_id,
    )
    workbench = CollaborationWorkbench(
      workbench_id=workbench_id,
      title=title,
      kind=kind,
      objective=objective,
      status=CollaborationWorkbenchStatus.CREATED,
      parent_run_id=parent_run_id,
      channel_id=channel.channel_id,
      member_ids=[member.member_id for member in members],
      metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
    )
    return workbench, channel, members

  def _build_members(self, raw_members: object, workbench_id: str) -> list[WorkbenchMember]:
    records = raw_members if isinstance(raw_members, list) and raw_members else _default_members()
    members: list[WorkbenchMember] = []
    for index, raw in enumerate(records):
      if not isinstance(raw, dict):
        raise ValueError("members must contain JSON objects.")
      role = _optional_str(raw.get("role")) or f"member_{index + 1}"
      participant_id = _optional_str(raw.get("participant_id")) or new_id("participant")
      kind = raw.get("kind") or (ParticipantKind.REMOTE_AGENT if raw.get("connector_id") else ParticipantKind.AGENT)
      labels = raw.get("labels", [])
      if not isinstance(labels, list) or not all(isinstance(item, str) for item in labels):
        raise ValueError("member.labels must be a list of strings.")
      metadata = raw.get("metadata")
      if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("member.metadata must be an object.")
      members.append(
        WorkbenchMember(
          member_id=_optional_str(raw.get("member_id")) or f"{workbench_id}_member_{index + 1}",
          participant_id=participant_id,
          kind=kind,
          role=role,
          agent_type=_optional_str(raw.get("agent_type")),
          connector_id=_optional_str(raw.get("connector_id")),
          agent_session_id=_optional_str(raw.get("agent_session_id")),
          labels=labels,
          metadata=metadata or {},
        )
      )
    return members

  def _build_task_slices(
    self,
    payload: dict[str, Any],
    workbench: CollaborationWorkbench,
    members: list[WorkbenchMember],
    *,
    default_slice_prefix: str,
  ) -> list[WorkbenchTaskSlice]:
    raw_slices = payload.get("slices") or payload.get("tasks")
    if raw_slices is None:
      raw_slices = [
        {
          "title": f"{default_slice_prefix}: {member.role}",
          "objective": workbench.objective,
          "assignee_member_id": member.member_id,
          "role": member.role,
        }
        for member in members
        if member.kind is not ParticipantKind.HUMAN
      ]
    if not isinstance(raw_slices, list) or not raw_slices:
      raise ValueError("slices/tasks must be a non-empty list.")
    by_member = {member.member_id: member for member in members}
    slices: list[WorkbenchTaskSlice] = []
    for index, raw in enumerate(raw_slices):
      if not isinstance(raw, dict):
        raise ValueError("slices/tasks must contain JSON objects.")
      assignee_member_id = _optional_str(raw.get("assignee_member_id"))
      if assignee_member_id is None:
        assignee_member_id = _pick_member_for_slice(members, index).member_id
      if assignee_member_id not in by_member:
        raise ValueError(f"Unknown assignee_member_id: {assignee_member_id}")
      title = _optional_str(raw.get("title")) or f"{default_slice_prefix} {index + 1}"
      objective = _optional_str(raw.get("objective")) or workbench.objective
      metadata = raw.get("metadata")
      if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("slice.metadata must be an object.")
      slices.append(
        WorkbenchTaskSlice(
          slice_id=_optional_str(raw.get("slice_id")) or f"{workbench.workbench_id}_slice_{index + 1}",
          title=title,
          objective=objective,
          role=_optional_str(raw.get("role")) or by_member[assignee_member_id].role,
          assignee_member_id=assignee_member_id,
          metadata=metadata or {},
        )
      )
    return slices

  def _build_review_slices(
    self,
    payload: dict[str, Any],
    workbench: CollaborationWorkbench,
    members: list[WorkbenchMember],
  ) -> list[WorkbenchTaskSlice]:
    raw_slices = payload.get("slices")
    if raw_slices is not None:
      return self._build_task_slices(payload, workbench, members, default_slice_prefix="评审任务")
    target = _optional_str(payload.get("target_ref")) or workbench.objective
    default_roles = ["architecture", "correctness", "security", "tests"]
    reviewer_members = [member for member in members if member.kind is not ParticipantKind.HUMAN] or members
    return [
      WorkbenchTaskSlice(
        slice_id=f"{workbench.workbench_id}_review_{index + 1}",
        title=f"技术评审: {role}",
        objective=f"Review {target} for {role}.",
        role=role,
        assignee_member_id=reviewer_members[index % len(reviewer_members)].member_id,
        status=WorkbenchTaskSliceStatus.REVIEW,
        metadata={"target_ref": target, "review_dimension": role},
      )
      for index, role in enumerate(default_roles)
    ]

  async def _maybe_start_delegations(
    self,
    payload: dict[str, Any],
    workbench: CollaborationWorkbench,
    members: list[WorkbenchMember],
    slices: list[WorkbenchTaskSlice],
  ) -> tuple[CollaborationWorkbench, list[WorkbenchTaskSlice]]:
    taskboard_item_ids: list[str] = []
    delegation_task_ids: list[str] = []
    by_member = {member.member_id: member for member in members}
    auto_start = bool(payload.get("auto_start", False))
    updated_slices: list[WorkbenchTaskSlice] = []
    for item in slices:
      board_item = self._taskboard.create_item(item.title)
      taskboard_item_ids.append(board_item.item_id)
      member = by_member.get(item.assignee_member_id or "")
      next_slice = replace(item, taskboard_item_id=board_item.item_id)
      if auto_start:
        if self._delegation_control is None:
          raise ValueError("auto_start requires a configured agent delegation broker.")
        if member is None or not member.connector_id:
          raise ValueError(f"Task slice requires an assignee member with connector_id: {item.slice_id}")
        report = await self._delegation_control.delegate(
          parent_run_id=workbench.parent_run_id or f"workbench:{workbench.workbench_id}",
          parent_agent_id=_optional_str(payload.get("parent_agent_id")),
          parent_session_id=_optional_str(payload.get("parent_session_id")),
          connector_id=member.connector_id,
          agent_type=member.agent_type,
          task=item.objective,
          metadata={
            "workbench_id": workbench.workbench_id,
            "workbench_kind": workbench.kind.value,
            "task_slice_id": item.slice_id,
            "role": item.role,
            **item.metadata,
          },
        )
        delegation_task_ids.append(report.task_id)
        next_slice = replace(
          next_slice,
          delegation_task_id=report.task_id,
          status=WorkbenchTaskSliceStatus.RUNNING,
          updated_at=utc_now(),
        )
      updated_slices.append(next_slice)
    workbench = replace(
      workbench,
      task_slice_ids=[item.slice_id for item in updated_slices],
      taskboard_item_ids=taskboard_item_ids,
      delegation_task_ids=delegation_task_ids,
      updated_at=utc_now(),
    )
    return workbench, updated_slices

  def _save_workbench(
    self,
    workbench: CollaborationWorkbench,
    members: list[WorkbenchMember],
    slices: list[WorkbenchTaskSlice],
  ) -> None:
    with self._uow_factory() as uow:
      uow.interactions.save_workbench(workbench)
      for member in members:
        uow.interactions.save_workbench_member(workbench.workbench_id, member)
      for item in slices:
        uow.interactions.save_workbench_task_slice(workbench.workbench_id, item)

  def _append_event(
    self,
    event_type: RuntimeEventType,
    workbench: CollaborationWorkbench,
    payload: dict[str, Any],
  ) -> None:
    with self._uow_factory() as uow:
      uow.events.append(
        RuntimeEvent(
          event_type=event_type,
          run_id=workbench.parent_run_id or f"workbench:{workbench.workbench_id}",
          payload={
            "workbench_id": workbench.workbench_id,
            "kind": workbench.kind.value,
            "status": workbench.status.value,
            **payload,
          },
        )
      )


def _default_members() -> list[dict[str, Any]]:
  return [
    {"participant_id": "human_user", "kind": ParticipantKind.HUMAN.value, "role": "human"},
    {"participant_id": "daily_agent", "kind": ParticipantKind.AGENT.value, "role": "moderator"},
  ]


def _permissions_for_member(member: WorkbenchMember) -> list[str]:
  permissions = ["chat"]
  if member.connector_id:
    permissions.extend(["delegate", "terminal"])
  if member.kind is ParticipantKind.HUMAN:
    permissions.append("approve")
  return permissions


def _pick_member_for_slice(members: list[WorkbenchMember], index: int) -> WorkbenchMember:
  candidates = [member for member in members if member.kind is not ParticipantKind.HUMAN] or members
  return candidates[index % len(candidates)]


def _speaker_policy(value: object) -> SpeakerPolicy:
  if value is None:
    return SpeakerPolicy(type=SpeakerPolicyType.ROUND_ROBIN)
  if isinstance(value, str):
    return SpeakerPolicy(type=SpeakerPolicyType(value))
  if not isinstance(value, dict):
    raise ValueError("speaker_policy must be a string or object.")
  policy_type = value.get("type", SpeakerPolicyType.ROUND_ROBIN.value)
  fixed_order = value.get("fixed_order", [])
  if not isinstance(fixed_order, list) or not all(isinstance(item, str) for item in fixed_order):
    raise ValueError("speaker_policy.fixed_order must be a list of strings.")
  return SpeakerPolicy(
    type=SpeakerPolicyType(policy_type),
    fixed_order=fixed_order,
    max_turns_per_participant=_optional_int(value.get("max_turns_per_participant"), field="max_turns_per_participant"),
    allow_user_interrupt=bool(value.get("allow_user_interrupt", True)),
    require_moderator_approval=bool(value.get("require_moderator_approval", False)),
  )


def _optional_str(value: object) -> str | None:
  if value is None:
    return None
  text = str(value)
  return text if text else None


def _required_str(value: object, field: str) -> str:
  text = _optional_str(value)
  if text is None:
    raise ValueError(f"{field} must be a non-empty string.")
  return text


def _optional_int(value: object, *, field: str) -> int | None:
  if value is None:
    return None
  try:
    return int(value)
  except (TypeError, ValueError) as exc:
    raise ValueError(f"{field} must be an integer.") from exc


def _positive_int(value: object, *, default: int, field: str) -> int:
  parsed = _optional_int(value, field=field)
  if parsed is None:
    return default
  if parsed <= 0:
    raise ValueError(f"{field} must be a positive integer.")
  return parsed
