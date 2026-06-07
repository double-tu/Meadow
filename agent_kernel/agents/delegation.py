"""Asynchronous agent delegation broker."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from typing import Any

from agent_kernel.agents.connectors import AgentConnector, ConnectorMessage
from agent_kernel.domain.base import new_id
from agent_kernel.domain.delegation import DelegationStatus, DelegationTask, DelegationTaskReport
from agent_kernel.domain.events import RuntimeEvent, RuntimeEventType
from agent_kernel.domain.identifiers import ArtifactRef


@dataclass(slots=True)
class AgentDelegationBrokerOptions:
  enabled: bool = True
  depth_limit: int | None = None
  fail_orphaned_running_on_recovery: bool = True
  result_artifact_threshold_chars: int = 4096


class AgentDelegationBroker:
  """Runs child-agent work through connectors while preserving parent scoped visibility."""

  def __init__(
    self,
    uow_factory,
    connectors: dict[str, AgentConnector],
    options: AgentDelegationBrokerOptions | None = None,
  ) -> None:
    self._uow_factory = uow_factory
    self._connectors = connectors
    self._options = options or AgentDelegationBrokerOptions()
    self._running: dict[str, asyncio.Task[None]] = {}
    self._signals: dict[str, asyncio.Event] = {}

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
    if connector_id not in self._connectors:
      raise KeyError(f"Connector not registered: {connector_id}")
    if not parent_run_id:
      raise ValueError("parent_run_id is required.")
    if not isinstance(task, str) or not task.strip():
      raise ValueError("task must be a non-empty string.")
    if metadata is not None and not isinstance(metadata, dict):
      raise ValueError("metadata must be a JSON object when provided.")
    child_depth = self._next_depth(metadata or {})
    if not self._options.enabled:
      raise ValueError("Agent delegation is disabled.")
    if self._options.depth_limit is not None and child_depth > self._options.depth_limit:
      raise ValueError(
        f"Agent delegation depth limit exceeded: child depth {child_depth} > {self._options.depth_limit}."
      )

    delegation = DelegationTask(
      task=task,
      parent_run_id=parent_run_id,
      parent_agent_id=parent_agent_id,
      parent_session_id=parent_session_id,
      connector_id=connector_id,
      connector_session_id=new_id("connector_session"),
      agent_type=agent_type,
      metadata={**(metadata or {}), "delegation_depth": child_depth},
    )
    connector = self._connectors[connector_id]
    await connector.start(
      delegation.connector_session_id,
      {
        "delegation_task_id": delegation.task_id,
        "parent_run_id": parent_run_id,
        "parent_agent_id": parent_agent_id,
        "agent_type": agent_type,
        **(metadata or {}),
      },
    )
    self._save_task_and_event(
      delegation,
      RuntimeEventType.AGENT_DELEGATION_STARTED,
      payload={"connector_id": connector_id, "agent_type": agent_type},
    )
    self._signals[delegation.task_id] = asyncio.Event()
    self._running[delegation.task_id] = asyncio.create_task(self._run_delegation(delegation.task_id))
    return DelegationTaskReport.from_task(delegation)

  async def get_status(
    self,
    *,
    parent_run_id: str,
    task_ids: list[str] | None = None,
    wait_ms: int | None = None,
  ) -> list[DelegationTaskReport]:
    if not parent_run_id:
      raise ValueError("parent_run_id is required.")
    selected_task_ids = task_ids or []
    if task_ids is not None and not all(isinstance(task_id, str) and task_id for task_id in task_ids):
      raise ValueError("task_ids must contain non-empty strings.")
    wait_seconds = self._wait_seconds(wait_ms)
    reports = self._snapshot(parent_run_id, selected_task_ids)
    if wait_seconds <= 0 or self._has_terminal(reports):
      return reports

    signals = [
      self._signals[report.task_id]
      for report in reports
      if report.status is DelegationStatus.RUNNING and report.task_id in self._signals
    ]
    if not signals:
      return reports
    wait_tasks = [asyncio.create_task(signal.wait()) for signal in signals]
    try:
      done, pending = await asyncio.wait(wait_tasks, timeout=wait_seconds, return_when=asyncio.FIRST_COMPLETED)
      for task in pending:
        task.cancel()
      if done:
        return self._snapshot(parent_run_id, selected_task_ids)
      return reports
    finally:
      for task in wait_tasks:
        if not task.done():
          task.cancel()

  async def cancel(
    self,
    *,
    parent_run_id: str,
    task_id: str,
    reason: str = "delegation cancelled",
  ) -> DelegationTaskReport:
    if not parent_run_id:
      raise ValueError("parent_run_id is required.")
    if not task_id:
      raise ValueError("task_id is required.")
    delegation = self._get_parent_scoped_task(parent_run_id, task_id)
    if delegation is None:
      return DelegationTaskReport.unknown(task_id)
    if delegation.is_terminal:
      return DelegationTaskReport.from_task(delegation)
    connector = self._connectors.get(delegation.connector_id)
    if connector is not None:
      await connector.stop(delegation.connector_session_id, reason)
    running = self._running.pop(task_id, None)
    if running is not None:
      running.cancel()
    delegation.mark_cancelled(reason)
    self._save_task_and_event(
      delegation,
      RuntimeEventType.AGENT_DELEGATION_CANCELLED,
      payload={"reason": reason, "connector_id": delegation.connector_id},
    )
    self._notify(task_id)
    return DelegationTaskReport.from_task(delegation)

  async def cancel_parent(
    self,
    *,
    parent_run_id: str,
    reason: str = "parent run cancelled",
  ) -> list[DelegationTaskReport]:
    if not parent_run_id:
      raise ValueError("parent_run_id is required.")
    reports = self._snapshot(parent_run_id, [])
    cancelled: list[DelegationTaskReport] = []
    for report in reports:
      if report.status is DelegationStatus.RUNNING:
        cancelled.append(
          await self.cancel(parent_run_id=parent_run_id, task_id=report.task_id, reason=reason)
        )
      else:
        cancelled.append(report)
    return cancelled

  def recover_orphaned_running(
    self,
    *,
    reason: str = "delegation process was not reattached after host restart",
  ) -> list[DelegationTaskReport]:
    """Mark persisted running delegations as failed when this broker cannot reattach them.

    The current connector protocol can start/send/stop sessions but does not yet
    expose a durable reattach operation. This conservative recovery prevents
    stale background tasks from staying `running` forever after host restart.
    """

    if not self._options.fail_orphaned_running_on_recovery:
      return []
    recovered: list[DelegationTaskReport] = []
    with self._uow_factory() as uow:
      tasks = uow.interactions.list_delegation_tasks()
    for task in tasks:
      if task.status is not DelegationStatus.RUNNING:
        continue
      if task.task_id in self._running:
        continue
      task.mark_failed(
        {
          "type": "orphaned_delegation",
          "message": reason,
          "recoverable": False,
        }
      )
      self._save_task_and_event(
        task,
        RuntimeEventType.AGENT_DELEGATION_FAILED,
        payload={
          "connector_id": task.connector_id,
          "error": task.error,
          "recovery": "orphaned_running_failed",
        },
      )
      self._notify(task.task_id)
      recovered.append(DelegationTaskReport.from_task(task))
    return recovered

  async def await_task(self, task_id: str) -> None:
    running = self._running.get(task_id)
    if running is not None:
      await running

  async def _run_delegation(self, task_id: str) -> None:
    delegation = self._get_task(task_id)
    if delegation is None:
      self._notify(task_id)
      return
    message = ConnectorMessage(
      message_id=new_id("connector_msg"),
      session_id=delegation.connector_session_id,
      content={
        "type": "delegation_task",
        "task_id": delegation.task_id,
        "task": delegation.task,
        "parent_run_id": delegation.parent_run_id,
        "parent_agent_id": delegation.parent_agent_id,
        "parent_session_id": delegation.parent_session_id,
        "agent_type": delegation.agent_type,
        "metadata": delegation.metadata,
      },
    )
    delegation.mark_running_message(message.message_id)
    self._save_task(delegation)
    try:
      connector = self._connectors[delegation.connector_id]
      turn = await connector.send(message)
    except asyncio.CancelledError:
      self._notify(task_id)
      raise
    except Exception as exc:
      current = self._get_task(task_id)
      if current is not None and not current.is_terminal:
        current.mark_failed({"type": "connector_error", "message": str(exc)})
        self._save_task_and_event(
          current,
          RuntimeEventType.AGENT_DELEGATION_FAILED,
          payload={"connector_id": current.connector_id, "error": current.error},
        )
      self._running.pop(task_id, None)
      self._notify(task_id)
      return

    current = self._get_task(task_id)
    if current is not None and not current.is_terminal:
      output, artifact_refs = self._prepare_completion_output(current, turn.output)
      current.mark_completed(turn_id=turn.turn_id, output=output, result_artifact_refs=artifact_refs)
      self._save_task_and_event(
        current,
        RuntimeEventType.AGENT_DELEGATION_COMPLETED,
        payload={
          "connector_id": current.connector_id,
          "turn_id": turn.turn_id,
          "completed": turn.completed,
          "result_artifact_refs": [ref.to_dict() for ref in artifact_refs],
        },
        artifact_refs=artifact_refs,
      )
    self._running.pop(task_id, None)
    self._notify(task_id)

  def _snapshot(self, parent_run_id: str, task_ids: list[str]) -> list[DelegationTaskReport]:
    if task_ids:
      return [
        DelegationTaskReport.from_task(task)
        if (task := self._get_parent_scoped_task(parent_run_id, task_id)) is not None
        else DelegationTaskReport.unknown(task_id)
        for task_id in task_ids
      ]
    with self._uow_factory() as uow:
      tasks = uow.interactions.list_delegation_tasks_by_parent(parent_run_id)
    return [DelegationTaskReport.from_task(task) for task in tasks]

  def _get_parent_scoped_task(self, parent_run_id: str, task_id: str) -> DelegationTask | None:
    task = self._get_task(task_id)
    if task is None or task.parent_run_id != parent_run_id:
      return None
    return task

  def _get_task(self, task_id: str) -> DelegationTask | None:
    with self._uow_factory() as uow:
      return uow.interactions.get_delegation_task(task_id)

  def _save_task(self, task: DelegationTask) -> None:
    with self._uow_factory() as uow:
      uow.interactions.save_delegation_task(task)

  def _save_task_and_event(
    self,
    task: DelegationTask,
    event_type: RuntimeEventType,
    *,
    payload: dict[str, Any],
    artifact_refs: list[ArtifactRef] | None = None,
  ) -> None:
    with self._uow_factory() as uow:
      uow.interactions.save_delegation_task(task)
      uow.events.append(
        RuntimeEvent(
          event_type=event_type,
          run_id=task.parent_run_id,
          agent_id=task.parent_agent_id,
          task_id=task.task_id,
          payload={
            "delegation_task_id": task.task_id,
            "status": task.status.value,
            **payload,
          },
          artifact_refs=artifact_refs or [],
        )
      )

  def _notify(self, task_id: str) -> None:
    signal = self._signals.get(task_id)
    if signal is not None:
      signal.set()

  @staticmethod
  def _has_terminal(reports: list[DelegationTaskReport]) -> bool:
    return any(report.status is not DelegationStatus.RUNNING for report in reports)

  @staticmethod
  def _wait_seconds(wait_ms: int | None) -> float:
    if wait_ms is None:
      return 0.0
    try:
      value = int(wait_ms)
    except (TypeError, ValueError) as exc:
      raise ValueError("wait_ms must be a non-negative integer.") from exc
    if value < 0:
      raise ValueError("wait_ms must be a non-negative integer.")
    return min(value, 30_000) / 1000

  @staticmethod
  def _next_depth(metadata: dict[str, Any]) -> int:
    raw_depth = metadata.get("delegation_depth", metadata.get("parent_delegation_depth", 0))
    try:
      parent_depth = int(raw_depth)
    except (TypeError, ValueError) as exc:
      raise ValueError("delegation_depth must be an integer when provided.") from exc
    if parent_depth < 0:
      raise ValueError("delegation_depth must be non-negative.")
    return parent_depth + 1

  def _prepare_completion_output(
    self,
    task: DelegationTask,
    output: dict[str, Any],
  ) -> tuple[dict[str, Any], list[ArtifactRef]]:
    serialized = json.dumps(output, ensure_ascii=False, sort_keys=True, default=str)
    threshold = max(0, int(self._options.result_artifact_threshold_chars))
    if threshold == 0 or len(serialized) <= threshold:
      return output, []
    artifact = ArtifactRef(
      artifact_id=new_id("art_delegation_result"),
      uri=f"artifact://delegations/{task.task_id}/result",
      media_type="application/json",
    )
    preview = serialized[: min(512, len(serialized))]
    with self._uow_factory() as uow:
      uow.artifacts.save(
        artifact,
        metadata={
          "kind": "delegation_result",
          "delegation_task_id": task.task_id,
          "parent_run_id": task.parent_run_id,
          "connector_id": task.connector_id,
          "agent_type": task.agent_type,
          "output_json": serialized,
          "output_size_chars": len(serialized),
          "preview": preview,
        },
      )
    return {
      "preview": preview,
      "truncated": True,
      "result_artifact_refs": [artifact.to_dict()],
    }, [artifact]
