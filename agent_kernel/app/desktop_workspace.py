"""Desktop workspace aggregation service.

The desktop host should not understand persistence tables directly. This service
builds UI-oriented snapshots from existing domain stores while keeping runtime
ownership inside the Python kernel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_kernel.domain.base import DomainModel


@dataclass(slots=True)
class DesktopWorkspaceSnapshot(DomainModel):
  workspace_id: str
  title: str
  task_ids: list[str] = field(default_factory=list)
  run_ids: list[str] = field(default_factory=list)
  agent_session_ids: list[str] = field(default_factory=list)
  channel_ids: list[str] = field(default_factory=list)
  artifact_ids: list[str] = field(default_factory=list)
  taskboard_item_ids: list[str] = field(default_factory=list)
  delegation_task_ids: list[str] = field(default_factory=list)
  pending_approval_ids: list[str] = field(default_factory=list)
  active_tool_call_ids: list[str] = field(default_factory=list)
  controls: dict[str, Any] = field(default_factory=dict)
  metadata: dict[str, Any] = field(default_factory=dict)


class DesktopWorkspaceService:
  def __init__(self, uow_factory) -> None:
    self._uow_factory = uow_factory

  def list_workspaces(self) -> list[DesktopWorkspaceSnapshot]:
    with self._uow_factory() as uow:
      runs = uow.states.list_all()
      channels = uow.interactions.list_channels()
      taskboard_items = uow.interactions.list_taskboard_items()
      agent_sessions = uow.agent_sessions.list_all()
      delegations = uow.interactions.list_delegation_tasks()
      artifacts = uow.artifacts.list_all()
      pending_approvals = uow.approvals.list_pending_all()
      tool_calls = uow.tool_calls.list_all()

    grouped: dict[str, DesktopWorkspaceSnapshot] = {}

    def ensure(workspace_id: str, title: str | None = None) -> DesktopWorkspaceSnapshot:
      if workspace_id not in grouped:
        grouped[workspace_id] = DesktopWorkspaceSnapshot(
          workspace_id=workspace_id,
          title=title or workspace_id,
          controls=_default_controls(),
        )
      elif title and grouped[workspace_id].title == workspace_id:
        grouped[workspace_id].title = title
      return grouped[workspace_id]

    run_to_workspace: dict[str, str] = {}
    task_to_workspace: dict[str, str] = {}
    for run in runs:
      workspace_id = _workspace_id_for(run.task_id, run.run_id)
      run_to_workspace[run.run_id] = workspace_id
      if run.task_id:
        task_to_workspace[run.task_id] = workspace_id
      snapshot = ensure(workspace_id, _workspace_title(run.task_id, run.run_id))
      _append_unique(snapshot.run_ids, run.run_id)
      if run.task_id:
        _append_unique(snapshot.task_ids, run.task_id)
      for ref in run.artifact_refs:
        _append_unique(snapshot.artifact_ids, ref.artifact_id)

    for channel in channels:
      workspace_id = (
        run_to_workspace.get(channel.bound_run_id or "")
        or task_to_workspace.get(channel.bound_task_id or "")
        or _workspace_id_for(channel.bound_task_id, channel.bound_run_id or channel.channel_id)
      )
      snapshot = ensure(workspace_id, channel.topic)
      _append_unique(snapshot.channel_ids, channel.channel_id)
      if channel.bound_task_id:
        _append_unique(snapshot.task_ids, channel.bound_task_id)
      if channel.bound_run_id:
        _append_unique(snapshot.run_ids, channel.bound_run_id)
      for ref in channel.bound_artifact_refs:
        _append_unique(snapshot.artifact_ids, ref.artifact_id)

    for item in taskboard_items:
      workspace_id = _first_workspace_for_task_ids(task_to_workspace, [item.item_id]) or "workspace_taskboard"
      snapshot = ensure(workspace_id, "Taskboard")
      _append_unique(snapshot.taskboard_item_ids, item.item_id)
      for ref in item.result_artifact_refs:
        _append_unique(snapshot.artifact_ids, ref.artifact_id)

    for session in agent_sessions:
      workspace_id = task_to_workspace.get(session.task_id or "") or _workspace_id_for(session.task_id, session.session_id)
      snapshot = ensure(workspace_id, _workspace_title(session.task_id, session.session_id))
      _append_unique(snapshot.agent_session_ids, session.session_id)
      if session.task_id:
        _append_unique(snapshot.task_ids, session.task_id)

    for delegation in delegations:
      workspace_id = run_to_workspace.get(delegation.parent_run_id) or _workspace_id_for(None, delegation.parent_run_id)
      snapshot = ensure(workspace_id, _workspace_title(None, delegation.parent_run_id))
      _append_unique(snapshot.delegation_task_ids, delegation.task_id)
      _append_unique(snapshot.run_ids, delegation.parent_run_id)

    for approval in pending_approvals:
      workspace_id = run_to_workspace.get(approval.run_id) or _workspace_id_for(None, approval.run_id)
      snapshot = ensure(workspace_id, _workspace_title(None, approval.run_id))
      _append_unique(snapshot.pending_approval_ids, approval.approval_id)
      _append_unique(snapshot.run_ids, approval.run_id)

    for call in tool_calls:
      workspace_id = run_to_workspace.get(call.run_id) or _workspace_id_for(None, call.run_id)
      snapshot = ensure(workspace_id, _workspace_title(None, call.run_id))
      if str(call.status) in {"running", "cancelling", "killing"}:
        _append_unique(snapshot.active_tool_call_ids, call.tool_call_id)
      _append_unique(snapshot.run_ids, call.run_id)

    if artifacts and not grouped:
      snapshot = ensure("workspace_artifacts", "Artifacts")
      for artifact in artifacts:
        _append_unique(snapshot.artifact_ids, artifact.artifact_id)

    return sorted(grouped.values(), key=lambda item: item.workspace_id)

  def get_workspace(self, workspace_id: str) -> DesktopWorkspaceSnapshot | None:
    return next((item for item in self.list_workspaces() if item.workspace_id == workspace_id), None)

  def list_pending_approvals(self, run_id: str | None = None) -> list[dict[str, Any]]:
    with self._uow_factory() as uow:
      approvals = uow.approvals.list_pending(run_id) if run_id else uow.approvals.list_pending_all()
    return [approval.to_dict() for approval in approvals]

  def list_tool_calls(self, run_id: str | None = None) -> list[dict[str, Any]]:
    with self._uow_factory() as uow:
      calls = uow.tool_calls.list_by_run(run_id) if run_id else uow.tool_calls.list_all()
    return [call.to_dict() for call in calls]


def _workspace_id_for(task_id: str | None, fallback_id: str) -> str:
  return f"workspace_task_{task_id}" if task_id else f"workspace_run_{fallback_id}"


def _workspace_title(task_id: str | None, fallback_id: str) -> str:
  return f"Task {task_id}" if task_id else f"Run {fallback_id}"


def _append_unique(items: list[str], value: str) -> None:
  if value not in items:
    items.append(value)


def _first_workspace_for_task_ids(mapping: dict[str, str], task_ids: list[str]) -> str | None:
  for task_id in task_ids:
    if task_id in mapping:
      return mapping[task_id]
  return None


def _default_controls() -> dict[str, Any]:
  return {
    "can_cancel_runs": True,
    "can_intervene": True,
    "can_approve": True,
    "can_cancel_tool_calls": True,
    "can_delegate": True,
  }
