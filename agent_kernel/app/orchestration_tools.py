"""Meadow orchestration capabilities exposed to Skill-planned chat."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

from agent_kernel.capabilities.adapters.local import LocalToolExecutor
from agent_kernel.capabilities.atomic import AtomicToolCall
from agent_kernel.capabilities.registry import CapabilityRegistry
from agent_kernel.domain.capability import CapabilitySpec, SideEffectLevel, ToolResult


class OrchestrationCapabilityIds:
  TASK_CREATE = "meadow.task.create"
  WORKFLOW_RUN = "meadow.workflow.run"
  TASK_STATUS = "meadow.task.status"
  TASK_CANCEL = "meadow.task.cancel"
  AGENT_PARALLEL_DELEGATE = "meadow.agent.parallel_delegate"
  WORKBENCH_CREATE = "meadow.workbench.create"
  WORKBENCH_STATUS = "meadow.workbench.status"
  WORKBENCH_MESSAGE = "meadow.workbench.message"
  WORKBENCH_DECISION = "meadow.workbench.decision"
  WORKBENCH_CANCEL = "meadow.workbench.cancel"


class TaskLauncher(Protocol):
  async def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
    ...


class RunControl(Protocol):
  def cancel_run(self, run_id: str, reason: str):
    ...


class UnitOfWorkFactory(Protocol):
  def __call__(self):
    ...


class AgentDelegationControl(Protocol):
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
  ):
    ...


class CollaborationWorkbenchControl(Protocol):
  def create_group_chat(self, payload: dict[str, Any]):
    ...

  async def create_cli_collaboration(self, payload: dict[str, Any]):
    ...

  async def create_parallel_delegation(self, payload: dict[str, Any]):
    ...

  async def create_technical_review(self, payload: dict[str, Any]):
    ...

  def snapshot(self, workbench_id: str):
    ...

  def send_message(self, workbench_id: str, payload: dict[str, Any]):
    ...

  def create_decision_artifact(self, workbench_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    ...

  async def cancel(self, workbench_id: str, reason: str = "workbench cancelled"):
    ...


@dataclass(slots=True)
class CompositeToolCatalog:
  catalogs: list[Any]

  def tool_schemas(self) -> list[dict[str, Any]]:
    schemas: list[dict[str, Any]] = []
    for catalog in self.catalogs:
      schemas.extend(catalog.tool_schemas())
    return schemas

  def normalize_call(self, name: str, input: dict[str, Any], *, run_id: str, scope: str) -> AtomicToolCall:
    for catalog in self.catalogs:
      can_handle = getattr(catalog, "can_handle", None)
      if callable(can_handle) and can_handle(name):
        return catalog.normalize_call(name, input, run_id=run_id, scope=scope)
    return self.catalogs[0].normalize_call(name, input, run_id=run_id, scope=scope)


class OrchestrationCapabilityProvider:
  def __init__(
    self,
    *,
    uow_factory: UnitOfWorkFactory,
    task_launcher: TaskLauncher,
    run_control: RunControl | None = None,
    delegation_control: AgentDelegationControl | None = None,
    workbench_control: CollaborationWorkbenchControl | None = None,
  ) -> None:
    self._uow_factory = uow_factory
    self._task_launcher = task_launcher
    self._run_control = run_control
    self._delegation_control = delegation_control
    self._workbench_control = workbench_control
    self._aliases = {
      "task_create": OrchestrationCapabilityIds.TASK_CREATE,
      "workflow_run": OrchestrationCapabilityIds.WORKFLOW_RUN,
      "task_status": OrchestrationCapabilityIds.TASK_STATUS,
      "task_cancel": OrchestrationCapabilityIds.TASK_CANCEL,
      "agent_parallel_delegate": OrchestrationCapabilityIds.AGENT_PARALLEL_DELEGATE,
      "workbench_create": OrchestrationCapabilityIds.WORKBENCH_CREATE,
      "workbench_status": OrchestrationCapabilityIds.WORKBENCH_STATUS,
      "workbench_message": OrchestrationCapabilityIds.WORKBENCH_MESSAGE,
      "workbench_decision": OrchestrationCapabilityIds.WORKBENCH_DECISION,
      "workbench_cancel": OrchestrationCapabilityIds.WORKBENCH_CANCEL,
    }

  def can_handle(self, name: str) -> bool:
    return name in self._aliases or name in self._aliases.values()

  def register(self, registry: CapabilityRegistry, local_tools: LocalToolExecutor) -> None:
    for spec in self.capability_specs():
      registry.register(spec)
    local_tools.register(OrchestrationCapabilityIds.TASK_CREATE, self.create_task)
    local_tools.register(OrchestrationCapabilityIds.WORKFLOW_RUN, self.run_workflow)
    local_tools.register(OrchestrationCapabilityIds.TASK_STATUS, self.task_status)
    local_tools.register(OrchestrationCapabilityIds.TASK_CANCEL, self.cancel_task)
    local_tools.register(OrchestrationCapabilityIds.AGENT_PARALLEL_DELEGATE, self.parallel_delegate)
    local_tools.register(OrchestrationCapabilityIds.WORKBENCH_CREATE, self.create_workbench)
    local_tools.register(OrchestrationCapabilityIds.WORKBENCH_STATUS, self.workbench_status)
    local_tools.register(OrchestrationCapabilityIds.WORKBENCH_MESSAGE, self.workbench_message)
    local_tools.register(OrchestrationCapabilityIds.WORKBENCH_DECISION, self.workbench_decision)
    local_tools.register(OrchestrationCapabilityIds.WORKBENCH_CANCEL, self.workbench_cancel)

  def capability_specs(self) -> list[CapabilitySpec]:
    return [
      CapabilitySpec(
        capability_id=OrchestrationCapabilityIds.TASK_CREATE,
        name="Create Meadow task",
        kind="tool",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        side_effect_level=SideEffectLevel.EXTERNAL_MUTATION,
      ),
      CapabilitySpec(
        capability_id=OrchestrationCapabilityIds.WORKFLOW_RUN,
        name="Run Meadow workflow",
        kind="tool",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        side_effect_level=SideEffectLevel.EXTERNAL_MUTATION,
      ),
      CapabilitySpec(
        capability_id=OrchestrationCapabilityIds.TASK_STATUS,
        name="Inspect Meadow task status",
        kind="tool",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        side_effect_level=SideEffectLevel.READ,
      ),
      CapabilitySpec(
        capability_id=OrchestrationCapabilityIds.TASK_CANCEL,
        name="Cancel Meadow task",
        kind="tool",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        side_effect_level=SideEffectLevel.EXTERNAL_MUTATION,
      ),
      CapabilitySpec(
        capability_id=OrchestrationCapabilityIds.AGENT_PARALLEL_DELEGATE,
        name="Parallel agent delegation",
        kind="tool",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        side_effect_level=SideEffectLevel.EXTERNAL_MUTATION,
      ),
      CapabilitySpec(
        capability_id=OrchestrationCapabilityIds.WORKBENCH_CREATE,
        name="Create collaboration workbench",
        kind="tool",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        side_effect_level=SideEffectLevel.EXTERNAL_MUTATION,
      ),
      CapabilitySpec(
        capability_id=OrchestrationCapabilityIds.WORKBENCH_STATUS,
        name="Inspect collaboration workbench",
        kind="tool",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        side_effect_level=SideEffectLevel.READ,
      ),
      CapabilitySpec(
        capability_id=OrchestrationCapabilityIds.WORKBENCH_MESSAGE,
        name="Send collaboration workbench message",
        kind="tool",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        side_effect_level=SideEffectLevel.EXTERNAL_MUTATION,
      ),
      CapabilitySpec(
        capability_id=OrchestrationCapabilityIds.WORKBENCH_DECISION,
        name="Create collaboration workbench decision artifact",
        kind="tool",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        side_effect_level=SideEffectLevel.WRITE,
      ),
      CapabilitySpec(
        capability_id=OrchestrationCapabilityIds.WORKBENCH_CANCEL,
        name="Cancel collaboration workbench",
        kind="tool",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        side_effect_level=SideEffectLevel.EXTERNAL_MUTATION,
      ),
    ]

  def tool_schemas(self) -> list[dict[str, Any]]:
    return [
      self._schema(
        "task_create",
        "Create and optionally start a Meadow runtime task. Use for substantial work that should be tracked as a task.",
        ["title"],
        {
          "run_id": {"type": "string"},
          "input": {"type": "object"},
          "start": {"type": "boolean"},
        },
      ),
      self._schema(
        "workflow_run",
        "Run a known or ad-hoc Meadow workflow entry point. Use when the task matches a workflow or should be tracked as workflow execution.",
        ["title"],
        {
          "workflow_id": {"type": "string"},
          "run_id": {"type": "string"},
          "input": {"type": "object"},
          "start": {"type": "boolean"},
        },
      ),
      self._schema("task_status", "Inspect a Meadow task/run status by run_id.", ["run_id"]),
      self._schema("task_cancel", "Cancel a Meadow task/run by run_id.", ["run_id"], {"reason": {"type": "string"}}),
      self._schema(
        "agent_parallel_delegate",
        "Start multiple child-agent tasks concurrently through configured connectors.",
        ["tasks"],
        {
          "parent_run_id": {"type": "string"},
          "tasks": {
            "type": "array",
            "items": {
              "type": "object",
              "properties": {
                "connector_id": {"type": "string"},
                "agent_type": {"type": "string"},
                "task": {"type": "string"},
                "metadata": {"type": "object"},
              },
              "required": ["connector_id", "task"],
            },
          },
        },
      ),
      self._schema(
        "workbench_create",
        (
          "Create a collaboration workbench for group chat, multi-CLI collaboration, technical review, "
          "or parallel child-agent delegation. Use this when work should continue asynchronously, needs a moderator, "
          "needs human participation, or needs multiple agents/CLI sessions."
        ),
        ["kind", "objective"],
        {
          "kind": {
            "type": "string",
            "enum": ["group_chat", "cli_collaboration", "technical_review", "parallel_delegation"],
          },
          "title": {"type": "string"},
          "objective": {"type": "string"},
          "parent_run_id": {"type": "string"},
          "auto_start": {"type": "boolean"},
          "members": {
            "type": "array",
            "items": {
              "type": "object",
              "properties": {
                "participant_id": {"type": "string"},
                "kind": {"type": "string", "enum": ["human", "agent", "remote_agent", "observer"]},
                "role": {"type": "string"},
                "connector_id": {"type": "string"},
                "agent_type": {"type": "string"},
                "labels": {"type": "array", "items": {"type": "string"}},
              },
            },
          },
          "slices": {
            "type": "array",
            "items": {
              "type": "object",
              "properties": {
                "title": {"type": "string"},
                "objective": {"type": "string"},
                "role": {"type": "string"},
                "assignee_member_id": {"type": "string"},
                "metadata": {"type": "object"},
              },
            },
          },
          "target_ref": {"type": "string"},
          "metadata": {"type": "object"},
        },
      ),
      self._schema(
        "workbench_status",
        "Inspect a collaboration workbench, including messages, members, task slices, taskboard items, and delegation statuses.",
        ["workbench_id"],
      ),
      self._schema(
        "workbench_message",
        (
          "Send a message into a workbench channel. Use this to act as moderator, user proxy, reviewer, "
          "or coordinator and continue driving an async group/CLI task."
        ),
        ["workbench_id", "sender_participant_id"],
        {
          "text": {"type": "string"},
          "content": {"type": "object"},
          "sender_participant_id": {"type": "string"},
        },
      ),
      self._schema(
        "workbench_decision",
        "Create a decision artifact from a group chat workbench once enough discussion/results exist.",
        ["workbench_id"],
        {"decided_by_participant_id": {"type": "string"}},
      ),
      self._schema(
        "workbench_cancel",
        "Cancel a collaboration workbench and any child delegations it owns.",
        ["workbench_id"],
        {"reason": {"type": "string"}},
      ),
    ]

  def normalize_call(self, name: str, input: dict[str, Any], *, run_id: str, scope: str) -> AtomicToolCall:
    capability_id = self._aliases.get(name, name)
    normalized = dict(input)
    normalized.setdefault("run_id", run_id)
    normalized.setdefault("scope", scope)
    if capability_id == OrchestrationCapabilityIds.AGENT_PARALLEL_DELEGATE:
      normalized.setdefault("parent_run_id", run_id)
      normalized.setdefault("parent_session_id", scope)
    if capability_id == OrchestrationCapabilityIds.WORKBENCH_CREATE:
      normalized.setdefault("parent_run_id", run_id)
      normalized.setdefault("metadata", {})
      if isinstance(normalized["metadata"], dict):
        normalized["metadata"].setdefault("created_from_scope", scope)
    return AtomicToolCall(capability_id=capability_id, input=normalized, display_name=name)

  async def create_task(self, input: dict[str, Any]) -> ToolResult:
    title = input.get("title")
    if not isinstance(title, str) or not title.strip():
      return ToolResult.failure("invalid_input", "title must be a non-empty string.")
    payload = {
      "title": title,
      "run_id": input.get("run_id"),
      "input": input.get("input") if isinstance(input.get("input"), dict) else {},
      "start": bool(input.get("start", True)),
    }
    result = await self._task_launcher.create_task(payload)
    return ToolResult.success({"task_result": result})

  async def run_workflow(self, input: dict[str, Any]) -> ToolResult:
    title = input.get("title") or input.get("workflow_id") or "workflow task"
    if not isinstance(title, str) or not title.strip():
      return ToolResult.failure("invalid_input", "title or workflow_id must be a non-empty string.")
    payload_input = input.get("input") if isinstance(input.get("input"), dict) else {}
    if isinstance(input.get("workflow_id"), str):
      payload_input = {**payload_input, "workflow_id": input["workflow_id"]}
    result = await self._task_launcher.create_task(
      {
        "title": title,
        "run_id": input.get("run_id"),
        "input": payload_input,
        "start": bool(input.get("start", True)),
      }
    )
    return ToolResult.success({"workflow_result": result})

  def task_status(self, input: dict[str, Any]) -> ToolResult:
    run_id = input.get("run_id")
    if not isinstance(run_id, str) or not run_id:
      return ToolResult.failure("invalid_input", "run_id must be a non-empty string.")
    with self._uow_factory() as uow:
      state = uow.states.get(run_id)
      events = uow.events.list_by_run(run_id)
    if state is None:
      return ToolResult.failure("task_not_found", f"Run not found: {run_id}")
    return ToolResult.success(
      {
        "run": state.to_dict(),
        "recent_events": [event.to_dict() for event in events[-10:]],
      }
    )

  def cancel_task(self, input: dict[str, Any]) -> ToolResult:
    if self._run_control is None:
      return ToolResult.failure("adapter_not_configured", "Run control is not configured.")
    run_id = input.get("run_id")
    if not isinstance(run_id, str) or not run_id:
      return ToolResult.failure("invalid_input", "run_id must be a non-empty string.")
    run = self._run_control.cancel_run(run_id, str(input.get("reason") or "cancelled by desktop chat"))
    return ToolResult.success({"run": run.to_dict()})

  async def parallel_delegate(self, input: dict[str, Any]) -> ToolResult:
    if self._delegation_control is None:
      if self._workbench_control is None:
        return ToolResult.failure("adapter_not_configured", "Agent delegation broker is not configured.")
      degraded = self._parallel_delegate_workbench_payload(input)
      if isinstance(degraded, ToolResult):
        return degraded
      try:
        snapshot = await self._workbench_control.create_parallel_delegation(degraded)
      except (KeyError, ValueError) as exc:
        return ToolResult.failure("workbench_error", str(exc))
      view = _workbench_view(snapshot)
      view["delegation_degraded"] = True
      view["delegation_degraded_reason"] = "Agent delegation broker is not configured."
      return ToolResult.success(
        {
          "workbench": view,
          "delegation_status": {
            "started": False,
            "reason": "agent_delegation_broker_not_configured",
            "next_step": "Configure agent connectors or continue this workbench with human/agent participants.",
          },
        },
        metadata={"degraded": True, "reason": "agent_delegation_broker_not_configured"},
      )
    parent_run_id = input.get("parent_run_id")
    if not isinstance(parent_run_id, str) or not parent_run_id:
      return ToolResult.failure("invalid_input", "parent_run_id must be a non-empty string.")
    tasks = input.get("tasks")
    if not isinstance(tasks, list) or not tasks:
      return ToolResult.failure("invalid_input", "tasks must be a non-empty array.")
    coroutines = []
    for item in tasks:
      if not isinstance(item, dict):
        return ToolResult.failure("invalid_input", "each task must be an object.")
      connector_id = item.get("connector_id")
      task = item.get("task")
      if not isinstance(connector_id, str) or not connector_id or not isinstance(task, str) or not task:
        return ToolResult.failure("invalid_input", "each task requires connector_id and task.")
      metadata = item.get("metadata")
      coroutines.append(
        self._delegation_control.delegate(
          parent_run_id=parent_run_id,
          parent_agent_id=str(input["parent_agent_id"]) if isinstance(input.get("parent_agent_id"), str) else None,
          parent_session_id=str(input["parent_session_id"]) if isinstance(input.get("parent_session_id"), str) else None,
          connector_id=connector_id,
          agent_type=str(item["agent_type"]) if isinstance(item.get("agent_type"), str) else None,
          task=task,
          metadata=metadata if isinstance(metadata, dict) else None,
        )
      )
    reports = await asyncio.gather(*coroutines)
    return ToolResult.success({"delegations": [report.to_dict() for report in reports]})

  def _parallel_delegate_workbench_payload(self, input: dict[str, Any]) -> dict[str, Any] | ToolResult:
    parent_run_id = input.get("parent_run_id")
    if not isinstance(parent_run_id, str) or not parent_run_id:
      return ToolResult.failure("invalid_input", "parent_run_id must be a non-empty string.")
    tasks = input.get("tasks")
    if not isinstance(tasks, list) or not tasks:
      return ToolResult.failure("invalid_input", "tasks must be a non-empty array.")
    members: list[dict[str, Any]] = []
    slices: list[dict[str, Any]] = []
    workbench_id = str(input.get("workbench_id") or "")
    for index, item in enumerate(tasks, start=1):
      if not isinstance(item, dict):
        return ToolResult.failure("invalid_input", "each task must be an object.")
      task = item.get("task")
      if not isinstance(task, str) or not task.strip():
        return ToolResult.failure("invalid_input", "each task requires task.")
      connector_id = item.get("connector_id")
      role = str(item.get("role") or f"worker_{index}")
      participant_id = str(item.get("participant_id") or f"pending_agent_{index}")
      member: dict[str, Any] = {
        "participant_id": participant_id,
        "kind": "remote_agent" if isinstance(connector_id, str) and connector_id else "agent",
        "role": role,
      }
      if isinstance(connector_id, str) and connector_id:
        member["connector_id"] = connector_id
      if isinstance(item.get("agent_type"), str):
        member["agent_type"] = item["agent_type"]
      members.append(member)
      metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
      slice_payload = {
        "title": str(item.get("title") or role),
        "objective": task,
        "role": role,
        "metadata": metadata,
      }
      if workbench_id:
        slice_payload["assignee_member_id"] = f"{workbench_id}_member_{index}"
      slices.append(slice_payload)
    objective = input.get("objective")
    return {
      "kind": "parallel_delegation",
      "title": str(input.get("title") or "并行子 Agent 任务"),
      "objective": objective if isinstance(objective, str) and objective.strip() else "并行执行多个子任务。",
      "parent_run_id": parent_run_id,
      "auto_start": False,
      "members": members,
      "slices": slices,
      "metadata": {
        **(input.get("metadata") if isinstance(input.get("metadata"), dict) else {}),
        "degraded_from": "agent_parallel_delegate",
        "degraded_reason": "agent_delegation_broker_not_configured",
      },
    }

  async def create_workbench(self, input: dict[str, Any]) -> ToolResult:
    if self._workbench_control is None:
      return ToolResult.failure("adapter_not_configured", "Collaboration workbench service is not configured.")
    kind = input.get("kind")
    if not isinstance(kind, str) or kind not in {
      "group_chat",
      "cli_collaboration",
      "technical_review",
      "parallel_delegation",
    }:
      return ToolResult.failure(
        "invalid_input",
        "kind must be group_chat, cli_collaboration, technical_review, or parallel_delegation.",
      )
    objective = input.get("objective")
    if not isinstance(objective, str) or not objective.strip():
      return ToolResult.failure("invalid_input", "objective must be a non-empty string.")
    try:
      if kind == "group_chat":
        snapshot = self._workbench_control.create_group_chat(input)
      elif kind == "cli_collaboration":
        snapshot = await self._workbench_control.create_cli_collaboration(input)
      elif kind == "technical_review":
        snapshot = await self._workbench_control.create_technical_review(input)
      else:
        snapshot = await self._workbench_control.create_parallel_delegation(input)
    except (KeyError, ValueError) as exc:
      if input.get("auto_start") is True and "delegation broker" in str(exc):
        degraded = {**input, "auto_start": False}
        degraded.setdefault("metadata", {})
        if isinstance(degraded["metadata"], dict):
          degraded["metadata"]["auto_start_degraded"] = True
          degraded["metadata"]["auto_start_degraded_reason"] = str(exc)
        try:
          if kind == "cli_collaboration":
            snapshot = await self._workbench_control.create_cli_collaboration(degraded)
          elif kind == "technical_review":
            snapshot = await self._workbench_control.create_technical_review(degraded)
          elif kind == "parallel_delegation":
            snapshot = await self._workbench_control.create_parallel_delegation(degraded)
          else:
            snapshot = self._workbench_control.create_group_chat(degraded)
        except (KeyError, ValueError) as degraded_exc:
          return ToolResult.failure("workbench_error", str(degraded_exc))
        view = _workbench_view(snapshot)
        view["auto_start_degraded"] = True
        view["auto_start_degraded_reason"] = str(exc)
        return ToolResult.success({"workbench": view})
      return ToolResult.failure("workbench_error", str(exc))
    return ToolResult.success({"workbench": _workbench_view(snapshot)})

  def workbench_status(self, input: dict[str, Any]) -> ToolResult:
    if self._workbench_control is None:
      return ToolResult.failure("adapter_not_configured", "Collaboration workbench service is not configured.")
    workbench_id = input.get("workbench_id")
    if not isinstance(workbench_id, str) or not workbench_id:
      return ToolResult.failure("invalid_input", "workbench_id must be a non-empty string.")
    try:
      snapshot = self._workbench_control.snapshot(workbench_id)
    except KeyError as exc:
      return ToolResult.failure("workbench_not_found", str(exc))
    return ToolResult.success({"workbench": _workbench_view(snapshot)})

  def workbench_message(self, input: dict[str, Any]) -> ToolResult:
    if self._workbench_control is None:
      return ToolResult.failure("adapter_not_configured", "Collaboration workbench service is not configured.")
    workbench_id = input.get("workbench_id")
    if not isinstance(workbench_id, str) or not workbench_id:
      return ToolResult.failure("invalid_input", "workbench_id must be a non-empty string.")
    payload = {
      "sender_participant_id": input.get("sender_participant_id"),
      "text": input.get("text"),
      "content": input.get("content") if isinstance(input.get("content"), dict) else None,
    }
    try:
      message = self._workbench_control.send_message(workbench_id, payload)
      snapshot = self._workbench_control.snapshot(workbench_id)
    except (KeyError, ValueError) as exc:
      return ToolResult.failure("workbench_error", str(exc))
    return ToolResult.success({"message": message.to_dict(), "workbench": _workbench_view(snapshot)})

  def workbench_decision(self, input: dict[str, Any]) -> ToolResult:
    if self._workbench_control is None:
      return ToolResult.failure("adapter_not_configured", "Collaboration workbench service is not configured.")
    workbench_id = input.get("workbench_id")
    if not isinstance(workbench_id, str) or not workbench_id:
      return ToolResult.failure("invalid_input", "workbench_id must be a non-empty string.")
    try:
      result = self._workbench_control.create_decision_artifact(
        workbench_id,
        {
          "decided_by_participant_id": input.get("decided_by_participant_id"),
        },
      )
    except (KeyError, ValueError) as exc:
      return ToolResult.failure("workbench_error", str(exc))
    return ToolResult.success(result)

  async def workbench_cancel(self, input: dict[str, Any]) -> ToolResult:
    if self._workbench_control is None:
      return ToolResult.failure("adapter_not_configured", "Collaboration workbench service is not configured.")
    workbench_id = input.get("workbench_id")
    if not isinstance(workbench_id, str) or not workbench_id:
      return ToolResult.failure("invalid_input", "workbench_id must be a non-empty string.")
    try:
      snapshot = await self._workbench_control.cancel(
        workbench_id,
        reason=str(input.get("reason") or "cancelled by agent"),
      )
    except (KeyError, ValueError) as exc:
      return ToolResult.failure("workbench_error", str(exc))
    return ToolResult.success({"workbench": _workbench_view(snapshot)})

  @staticmethod
  def _schema(
    name: str,
    description: str,
    required: list[str],
    properties: dict[str, Any] | None = None,
  ) -> dict[str, Any]:
    merged = dict(properties or {})
    for key in required:
      merged.setdefault(key, {"type": "string"})
    return {
      "type": "function",
      "function": {
        "name": name,
        "description": description,
        "parameters": {
          "type": "object",
          "properties": merged,
          "required": required,
        },
      },
    }


def _workbench_view(snapshot: Any) -> dict[str, Any]:
  workbench = snapshot.workbench
  messages = snapshot.messages[-8:] if getattr(snapshot, "messages", None) else []
  return {
    "workbench": workbench.to_dict(),
    "channel": snapshot.channel.to_dict() if snapshot.channel is not None else None,
    "group_chat": snapshot.group_chat,
    "members": [member.to_dict() for member in snapshot.members],
    "task_slices": [item.to_dict() for item in snapshot.task_slices],
    "delegations": snapshot.delegations,
    "taskboard_items": snapshot.taskboard_items,
    "recent_messages": [message.to_dict() for message in messages],
  }
