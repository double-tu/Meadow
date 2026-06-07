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
  ) -> None:
    self._uow_factory = uow_factory
    self._task_launcher = task_launcher
    self._run_control = run_control
    self._delegation_control = delegation_control
    self._aliases = {
      "task_create": OrchestrationCapabilityIds.TASK_CREATE,
      "workflow_run": OrchestrationCapabilityIds.WORKFLOW_RUN,
      "task_status": OrchestrationCapabilityIds.TASK_STATUS,
      "task_cancel": OrchestrationCapabilityIds.TASK_CANCEL,
      "agent_parallel_delegate": OrchestrationCapabilityIds.AGENT_PARALLEL_DELEGATE,
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
    ]

  def normalize_call(self, name: str, input: dict[str, Any], *, run_id: str, scope: str) -> AtomicToolCall:
    capability_id = self._aliases.get(name, name)
    normalized = dict(input)
    normalized.setdefault("run_id", run_id)
    normalized.setdefault("scope", scope)
    if capability_id == OrchestrationCapabilityIds.AGENT_PARALLEL_DELEGATE:
      normalized.setdefault("parent_run_id", run_id)
      normalized.setdefault("parent_session_id", scope)
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
      return ToolResult.failure("adapter_not_configured", "Agent delegation broker is not configured.")
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
