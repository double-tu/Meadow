"""HTTP host for runtime inspection and control."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
from typing import Any, Protocol
from urllib.parse import parse_qs, unquote, urlparse

from agent_kernel.agents import AgentDelegationBroker, ProductCLIConnectorFactory, load_product_cli_connector_specs
from agent_kernel.app.collaboration_workbench import CollaborationWorkbenchService
from agent_kernel.app.tool_call_control import ToolCallControlOutcome, ToolCallControlService
from agent_kernel.app.config_center import ConfigCenterService
from agent_kernel.app.conversation_task_hub import ConversationTaskHub
from agent_kernel.app.control_plane import ControlPlaneService
from agent_kernel.app.desktop_chat import DesktopChatService
from agent_kernel.app.desktop_workspace import DesktopWorkspaceService
from agent_kernel.app.mcp_config import MCPConfigService
from agent_kernel.app.orchestration_tools import (
  CompositeToolCatalog,
  OrchestrationCapabilityIds,
  OrchestrationCapabilityProvider,
)
from agent_kernel.app.scheduled_tasks import ScheduledTaskService
from agent_kernel.autonomy import SkillService
from agent_kernel.autonomy.builtin_skills import ensure_builtin_atomic_skills
from agent_kernel.capabilities import AtomicCapabilityIds, AtomicCapabilityProvider, CapabilityRegistry, CapabilityRuntime
from agent_kernel.capabilities.adapters import LocalFileWorkspace, LocalToolExecutor, UrllibHTTPClient
from agent_kernel.domain.delegation import DelegationTaskReport
from agent_kernel.domain.errors import DomainError
from agent_kernel.domain.policy import ApprovalRequest
from agent_kernel.domain.run import RunState
from agent_kernel.domain.capability import CapabilityGrant
from agent_kernel.domain.workflow import EdgeSpec, ExecutionCommand, NodeContext, NodeResult, NodeSpec, WorkflowSpec
from agent_kernel.domain.base import utc_now
from agent_kernel.hosts.dto import EventStreamEnvelope, error_response, ok_response
from agent_kernel.config import LLMConfig
from agent_kernel.memory import MemoryFacade
from agent_kernel.persistence import UnitOfWork, connect_sqlite
from agent_kernel.policy import ApprovalService, HumanInterventionService, InterventionOutcome
from agent_kernel.policy.engine import PolicyEngine
from agent_kernel.runtime import RuntimeEngine, unit_of_work_factory
from agent_kernel.workflow import FunctionNodeExecutor, NodeExecutorRegistry


class TaskLauncher(Protocol):
  async def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
    """Create a task entry point and optionally start its run."""


class RunControl(Protocol):
  def cancel_run(self, run_id: str, reason: str) -> RunState:
    """Cancel a run through the runtime control path."""


class ApprovalControl(Protocol):
  def approve(self, approval_id: str, ttl_seconds: int = 300) -> CapabilityGrant:
    """Approve a pending approval request."""

  def reject(self, approval_id: str) -> ApprovalRequest:
    """Reject a pending approval request."""


class InterventionControl(Protocol):
  def apply(
    self,
    run_id: str,
    content: str,
    intervention_type: str = "correction",
    apply_mode: str = "continue_next_turn",
    thread_id: str | None = None,
    task_id: str | None = None,
    priority: str = "high",
    memory_scope: str | None = None,
  ) -> InterventionOutcome:
    """Apply a human intervention through the policy service."""


class ToolCallControl(Protocol):
  async def cancel(self, tool_call_id: str, grace_seconds: float = 1.0) -> ToolCallControlOutcome:
    """Request cancellation of a tool call."""

  async def kill(self, tool_call_id: str) -> ToolCallControlOutcome:
    """Request kill of a tool call."""


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
  ) -> DelegationTaskReport:
    """Start a child-agent delegation task."""

  async def get_status(
    self,
    *,
    parent_run_id: str,
    task_ids: list[str] | None = None,
    wait_ms: int | None = None,
  ) -> list[DelegationTaskReport]:
    """Inspect parent-scoped delegation task status."""

  async def cancel(
    self,
    *,
    parent_run_id: str,
    task_id: str,
    reason: str = "delegation cancelled",
  ) -> DelegationTaskReport:
    """Cancel a running child-agent delegation task."""


class HTTPHost:
  def __init__(
    self,
    uow_factory,
    *,
    run_control: RunControl | None = None,
    approval_control: ApprovalControl | None = None,
    intervention_control: InterventionControl | None = None,
    tool_call_control: ToolCallControl | None = None,
    delegation_control: AgentDelegationControl | None = None,
    task_launcher: TaskLauncher | None = None,
    mcp_config_service: MCPConfigService | None = None,
    scheduled_task_service: ScheduledTaskService | None = None,
    control_plane: ControlPlaneService | None = None,
    desktop_workspace_service: DesktopWorkspaceService | None = None,
    desktop_chat_service: DesktopChatService | None = None,
    collaboration_workbench_service: CollaborationWorkbenchService | None = None,
    config_center_service: ConfigCenterService | None = None,
    skill_service: SkillService | None = None,
    default_llm_config: LLMConfig | None = None,
  ) -> None:
    self._uow_factory = uow_factory
    self._run_control = run_control or RuntimeEngine(uow_factory, NodeExecutorRegistry())
    self._approval_control = approval_control or ApprovalService(uow_factory)
    self._intervention_control = intervention_control or HumanInterventionService(uow_factory)
    self._tool_call_control = tool_call_control or ToolCallControlService(uow_factory)
    self._delegation_control = delegation_control
    self._task_launcher = task_launcher or SampleWorkflowTaskLauncher(uow_factory)
    self._mcp_config_service = mcp_config_service or MCPConfigService(uow_factory)
    self._scheduled_task_service = scheduled_task_service or ScheduledTaskService(
      uow_factory,
      self._task_launcher,
    )
    self._control_plane = control_plane or ControlPlaneService.from_config({"browser": {"enabled": False}})
    self._desktop_workspace_service = desktop_workspace_service or DesktopWorkspaceService(uow_factory)
    self._collaboration_workbench_service = collaboration_workbench_service or CollaborationWorkbenchService(
      uow_factory,
      delegation_control=delegation_control,
    )
    self._config_center_service = config_center_service or ConfigCenterService(uow_factory)
    self._skill_service = skill_service or SkillService(uow_factory)
    ensure_builtin_atomic_skills(self._skill_service)
    atomic_capabilities = AtomicCapabilityProvider(
      file_workspace=LocalFileWorkspace(["."]),
      http_client=UrllibHTTPClient(),
      memory=MemoryFacade(uow_factory),
      delegation=delegation_control,
      uow_factory=uow_factory,
      skill_service=self._skill_service,
    )
    orchestration_capabilities = OrchestrationCapabilityProvider(
      uow_factory=uow_factory,
      task_launcher=self._task_launcher,
      run_control=self._run_control,
      delegation_control=delegation_control,
      workbench_control=self._collaboration_workbench_service,
    )
    capability_registry = CapabilityRegistry()
    local_tools = LocalToolExecutor()
    atomic_capabilities.register(capability_registry, local_tools)
    orchestration_capabilities.register(capability_registry, local_tools)
    capability_runtime = CapabilityRuntime(
      capability_registry,
      PolicyEngine(grants=_default_desktop_atomic_grants()),
      local_tools,
      control_workbench=self._control_plane.workbench,
      uow_factory=uow_factory,
    )
    self._desktop_chat_service = desktop_chat_service or DesktopChatService(
      uow_factory,
      self._task_launcher,
      run_control=self._run_control,
      skill_service=self._skill_service,
      capability_runtime=capability_runtime,
      atomic_capabilities=CompositeToolCatalog([atomic_capabilities, orchestration_capabilities]),
      conversation_task_hub=ConversationTaskHub(uow_factory, self._task_launcher),
      default_llm_config=default_llm_config,
    )

  def inspect_run(self, run_id: str) -> dict[str, Any]:
    with self._uow_factory() as uow:
      state = uow.states.get(run_id)
    if state is None:
      raise KeyError(f"Run not found: {run_id}")
    return ok_response(run=state.to_dict())

  def list_run_events(self, run_id: str, *, after_event_id: str | None = None) -> list[EventStreamEnvelope]:
    with self._uow_factory() as uow:
      events = uow.events.list_by_run(run_id)
    if after_event_id:
      events = _events_after(events, after_event_id)
    return [_event_envelope(event) for event in events]

  def list_events(self, query: dict[str, list[str]] | None = None) -> list[EventStreamEnvelope]:
    parsed = query or {}
    run_id = _query_optional_str(parsed, "run_id")
    after_event_id = _query_optional_str(parsed, "after_event_id")
    with self._uow_factory() as uow:
      events = uow.events.list_by_run(run_id) if run_id else uow.events.list_all()
    if after_event_id:
      events = _events_after(events, after_event_id)
    return [
      _event_envelope(event)
      for event in events
    ]

  def inspect_artifact(self, artifact_id: str) -> dict[str, Any]:
    with self._uow_factory() as uow:
      artifact = uow.artifacts.get(artifact_id)
      metadata = uow.artifacts.get_metadata(artifact_id)
    if artifact is None:
      raise KeyError(f"Artifact not found: {artifact_id}")
    return ok_response(artifact=artifact.to_dict(), metadata=metadata or {})

  def cancel_run(self, run_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    reason = str((payload or {}).get("reason") or "user requested cancel")
    state = self._run_control.cancel_run(run_id, reason)
    return ok_response(run=state.to_dict())

  def apply_intervention(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
      raise ValueError("Intervention content is required.")
    outcome = self._intervention_control.apply(
      run_id=run_id,
      content=content,
      intervention_type=str(payload.get("type") or "correction"),
      apply_mode=str(payload.get("mode") or "continue_next_turn"),
      thread_id=_optional_str(payload.get("thread_id")),
      task_id=_optional_str(payload.get("task_id")),
      priority=str(payload.get("priority") or "high"),
      memory_scope=_optional_str(payload.get("memory_scope")),
    )
    return ok_response(
      intervention=outcome.intervention.to_dict(),
      run_status=outcome.run_status.value if hasattr(outcome.run_status, "value") else outcome.run_status,
      memory_id=outcome.memory_id,
      interrupted_step_id=outcome.interrupted_step_id,
    )

  def approve(self, approval_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    ttl_seconds = _positive_int((payload or {}).get("ttl_seconds"), default=300, field="ttl_seconds")
    grant = self._approval_control.approve(approval_id, ttl_seconds=ttl_seconds)
    return ok_response(grant=grant.to_dict())

  def reject(self, approval_id: str) -> dict[str, Any]:
    approval = self._approval_control.reject(approval_id)
    return ok_response(approval=approval.to_dict())

  async def cancel_tool_call(self, tool_call_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    grace_seconds = _positive_float(
      (payload or {}).get("grace_seconds"),
      default=1.0,
      field="grace_seconds",
    )
    outcome = await self._tool_call_control.cancel(tool_call_id, grace_seconds=grace_seconds)
    return _tool_call_control_response(outcome)

  async def kill_tool_call(self, tool_call_id: str) -> dict[str, Any]:
    outcome = await self._tool_call_control.kill(tool_call_id)
    return _tool_call_control_response(outcome)

  async def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
    return await self._task_launcher.create_task(payload)

  async def delegate_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
    control = self._require_delegation_control()
    metadata = payload.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
      raise ValueError("metadata must be a JSON object when provided.")
    report = await control.delegate(
      parent_run_id=_required_str(payload.get("parent_run_id"), "parent_run_id"),
      parent_agent_id=_optional_str(payload.get("parent_agent_id")),
      parent_session_id=_optional_str(payload.get("parent_session_id")),
      connector_id=_required_str(payload.get("connector_id"), "connector_id"),
      agent_type=_optional_str(payload.get("agent_type")),
      task=_required_str(payload.get("task"), "task"),
      metadata=metadata,
    )
    return ok_response(delegation=report.to_dict())

  async def get_delegation_status(
    self,
    parent_run_id: str,
    task_ids: list[str] | None = None,
    wait_ms: int | None = None,
  ) -> dict[str, Any]:
    control = self._require_delegation_control()
    reports = await control.get_status(parent_run_id=parent_run_id, task_ids=task_ids, wait_ms=wait_ms)
    return ok_response(delegations=[report.to_dict() for report in reports])

  async def cancel_delegation(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    control = self._require_delegation_control()
    report = await control.cancel(
      parent_run_id=_required_str(payload.get("parent_run_id"), "parent_run_id"),
      task_id=task_id,
      reason=str(payload.get("reason") or "delegation cancelled"),
    )
    return ok_response(delegation=report.to_dict())

  def list_mcp_servers(self, query: dict[str, list[str]] | None = None) -> dict[str, Any]:
    parsed = query or {}
    enabled_only = _query_bool(parsed, "enabled_only", default=False)
    agent_type = _query_optional_str(parsed, "agent_type")
    servers = self._mcp_config_service.list_servers(enabled_only=enabled_only, agent_type=agent_type)
    return ok_response(mcp_servers=[server.to_dict() for server in servers])

  def upsert_mcp_server(self, payload: dict[str, Any]) -> dict[str, Any]:
    server = self._mcp_config_service.upsert_server(payload)
    return ok_response(mcp_server=server.to_dict())

  def delete_mcp_server(self, name: str) -> dict[str, Any]:
    return ok_response(deleted=self._mcp_config_service.delete_server(name), name=name)

  def list_scheduled_tasks(self, query: dict[str, list[str]] | None = None) -> dict[str, Any]:
    enabled_only = _query_bool(query or {}, "enabled_only", default=False)
    tasks = self._scheduled_task_service.list(enabled_only=enabled_only)
    return ok_response(scheduled_tasks=[task.to_dict() for task in tasks])

  def get_scheduled_task_triggers(self, task_id: str) -> dict[str, Any]:
    if self._scheduled_task_service.get(task_id) is None:
      raise KeyError(f"Scheduled task not found: {task_id}")
    triggers = self._scheduled_task_service.list_triggers(task_id)
    return ok_response(triggers=[trigger.to_dict() for trigger in triggers])

  def create_scheduled_task(self, payload: dict[str, Any]) -> dict[str, Any]:
    task = self._scheduled_task_service.create(payload)
    return ok_response(scheduled_task=task.to_dict())

  def update_scheduled_task(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    task = self._scheduled_task_service.update(task_id, payload)
    return ok_response(scheduled_task=task.to_dict())

  def delete_scheduled_task(self, task_id: str) -> dict[str, Any]:
    return ok_response(deleted=self._scheduled_task_service.delete(task_id), task_id=task_id)

  async def run_due_scheduled_tasks(self, payload: dict[str, Any]) -> dict[str, Any]:
    limit = _optional_int(payload.get("limit"), field="limit")
    triggers = await self._scheduled_task_service.run_due(limit=limit)
    return ok_response(triggers=[trigger.to_dict() for trigger in triggers])

  async def control_health(self) -> dict[str, Any]:
    return await self._control_plane.health()

  def control_targets(self, query: dict[str, list[str]] | None = None) -> dict[str, Any]:
    raw_kind = _query_optional_str(query or {}, "kind")
    if raw_kind is not None and raw_kind not in {"browser", "desktop", "mobile"}:
      raise ValueError("kind must be browser, desktop, or mobile.")
    return ok_response(**self._control_plane.list_targets(raw_kind))

  async def execute_control_command(self, payload: dict[str, Any]) -> dict[str, Any]:
    return ok_response(**await self._control_plane.execute(payload))

  def list_chat_sessions(self) -> dict[str, Any]:
    return ok_response(chat_sessions=[session.to_dict() for session in self._desktop_chat_service.list_sessions()])

  def create_chat_session(self, payload: dict[str, Any]) -> dict[str, Any]:
    return ok_response(chat_session=self._desktop_chat_service.create_session(payload).to_dict())

  def list_chat_messages(self, session_id: str) -> dict[str, Any]:
    return ok_response(messages=[message.to_dict() for message in self._desktop_chat_service.list_messages(session_id)])

  async def send_chat_message(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return ok_response(**await self._desktop_chat_service.send_message(session_id, payload))

  async def retry_chat_message(self, session_id: str) -> dict[str, Any]:
    return ok_response(**await self._desktop_chat_service.retry_last(session_id))

  def clear_chat_messages(self, session_id: str) -> dict[str, Any]:
    return ok_response(**self._desktop_chat_service.clear_messages(session_id))

  def pause_chat_session(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return ok_response(**self._desktop_chat_service.pause(session_id, reason=str(payload.get("reason") or "user paused chat task")))

  def list_skills(self, query: dict[str, list[str]] | None = None) -> dict[str, Any]:
    active_only = _query_bool(query or {}, "active_only", default=False)
    skills = self._skill_service.list_active() if active_only else self._skill_service.list_all()
    return ok_response(skills=[skill.to_dict() for skill in skills])

  def create_skill(self, payload: dict[str, Any]) -> dict[str, Any]:
    skill = self._skill_service.create_interpreted_skill(
      name=_required_str(payload.get("name"), "name"),
      description=str(payload.get("description") or ""),
      when_to_use=str(payload.get("when_to_use") or ""),
      instructions=str(payload.get("instructions") or ""),
      recommended_tools=_optional_string_list(payload.get("recommended_tools"), field="recommended_tools") or [],
      recommended_workflows=_optional_string_list(payload.get("recommended_workflows"), field="recommended_workflows") or [],
    )
    if payload.get("status") == "active":
      skill = self._skill_service.activate(skill.skill_id)
    return ok_response(skill=skill.to_dict())

  def update_skill(self, skill_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return ok_response(skill=self._skill_service.update(skill_id, payload).to_dict())

  def activate_skill(self, skill_id: str) -> dict[str, Any]:
    return ok_response(skill=self._skill_service.activate(skill_id).to_dict())

  def deprecate_skill(self, skill_id: str) -> dict[str, Any]:
    return ok_response(skill=self._skill_service.deprecate(skill_id).to_dict())

  def list_config_sections(self) -> dict[str, Any]:
    return ok_response(config_sections=[section.to_dict() for section in self._config_center_service.list_sections()])

  def get_config_section(self, section: str) -> dict[str, Any]:
    return ok_response(config_section=self._config_center_service.get_section(section).to_dict())

  def update_config_section(self, section: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    merge = bool(payload.get("merge", True))
    return ok_response(config_section=self._config_center_service.update_section(section, data, merge=merge).to_dict())

  def list_workspaces(self) -> dict[str, Any]:
    return ok_response(workspaces=[snapshot.to_dict() for snapshot in self._desktop_workspace_service.list_workspaces()])

  def inspect_workspace(self, workspace_id: str) -> dict[str, Any]:
    snapshot = self._desktop_workspace_service.get_workspace(workspace_id)
    if snapshot is None:
      raise KeyError(f"Workspace not found: {workspace_id}")
    return ok_response(workspace=snapshot.to_dict())

  def list_collaboration_workbenches(self) -> dict[str, Any]:
    return ok_response(
      workbenches=[snapshot.to_dict() for snapshot in self._collaboration_workbench_service.list()]
    )

  def inspect_collaboration_workbench(self, workbench_id: str) -> dict[str, Any]:
    return ok_response(workbench=self._collaboration_workbench_service.snapshot(workbench_id).to_dict())

  def create_group_chat_workbench(self, payload: dict[str, Any]) -> dict[str, Any]:
    return ok_response(workbench=self._collaboration_workbench_service.create_group_chat(payload).to_dict())

  async def create_cli_workbench(self, payload: dict[str, Any]) -> dict[str, Any]:
    return ok_response(workbench=(await self._collaboration_workbench_service.create_cli_collaboration(payload)).to_dict())

  async def create_parallel_delegation_workbench(self, payload: dict[str, Any]) -> dict[str, Any]:
    return ok_response(
      workbench=(await self._collaboration_workbench_service.create_parallel_delegation(payload)).to_dict()
    )

  async def create_technical_review_workbench(self, payload: dict[str, Any]) -> dict[str, Any]:
    return ok_response(workbench=(await self._collaboration_workbench_service.create_technical_review(payload)).to_dict())

  def send_collaboration_workbench_message(self, workbench_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    message = self._collaboration_workbench_service.send_message(workbench_id, payload)
    return ok_response(message=message.to_dict(), workbench=self._collaboration_workbench_service.snapshot(workbench_id).to_dict())

  def create_collaboration_decision_artifact(self, workbench_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return self._collaboration_workbench_service.create_decision_artifact(workbench_id, payload)

  async def cancel_collaboration_workbench(self, workbench_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    snapshot = await self._collaboration_workbench_service.cancel(
      workbench_id,
      reason=str(payload.get("reason") or "user requested workbench cancel"),
    )
    return ok_response(workbench=snapshot.to_dict())

  def list_pending_approvals(self, query: dict[str, list[str]] | None = None) -> dict[str, Any]:
    run_id = _query_optional_str(query or {}, "run_id")
    return ok_response(approvals=self._desktop_workspace_service.list_pending_approvals(run_id))

  def list_tool_calls(self, query: dict[str, list[str]] | None = None) -> dict[str, Any]:
    run_id = _query_optional_str(query or {}, "run_id")
    return ok_response(tool_calls=self._desktop_workspace_service.list_tool_calls(run_id))

  def _require_delegation_control(self) -> AgentDelegationControl:
    if self._delegation_control is None:
      raise ValueError("Agent delegation broker is not configured.")
    return self._delegation_control


def make_handler(host: HTTPHost) -> type[BaseHTTPRequestHandler]:
  class AgentKernelHTTPRequestHandler(BaseHTTPRequestHandler):
    server_version = "AgentKernelHTTP/0.1"

    def do_OPTIONS(self) -> None:
      self.send_response(HTTPStatus.NO_CONTENT.value)
      self._write_common_headers()
      self.send_header("Content-Length", "0")
      self.end_headers()

    def do_GET(self) -> None:
      parsed = urlparse(self.path)
      path = parsed.path
      query = parse_qs(parsed.query)
      segments = [unquote(segment) for segment in path.split("/") if segment]
      try:
        if len(segments) == 2 and segments[0] == "runs":
          self._write_json(HTTPStatus.OK, host.inspect_run(segments[1]))
          return
        if len(segments) == 3 and segments[0] == "runs" and segments[2] == "events":
          envelopes = host.list_run_events(segments[1], after_event_id=_query_optional_str(query, "after_event_id"))
          if self._wants_sse(query):
            self._write_sse(HTTPStatus.OK, envelopes)
            return
          self._write_ndjson(HTTPStatus.OK, [envelope.to_dict() for envelope in envelopes])
          return
        if len(segments) == 1 and segments[0] == "events":
          envelopes = host.list_events(query)
          if self._wants_sse(query):
            self._write_sse(HTTPStatus.OK, envelopes)
            return
          self._write_ndjson(HTTPStatus.OK, [envelope.to_dict() for envelope in envelopes])
          return
        if len(segments) == 2 and segments[0] == "artifacts":
          self._write_json(HTTPStatus.OK, host.inspect_artifact(segments[1]))
          return
        if len(segments) == 1 and segments[0] == "workspaces":
          self._write_json(HTTPStatus.OK, host.list_workspaces())
          return
        if len(segments) == 2 and segments[0] == "workspaces":
          self._write_json(HTTPStatus.OK, host.inspect_workspace(segments[1]))
          return
        if len(segments) == 2 and segments[0] == "collaboration" and segments[1] == "workbenches":
          self._write_json(HTTPStatus.OK, host.list_collaboration_workbenches())
          return
        if len(segments) == 3 and segments[0] == "collaboration" and segments[1] == "workbenches":
          self._write_json(HTTPStatus.OK, host.inspect_collaboration_workbench(segments[2]))
          return
        if len(segments) == 2 and segments[0] == "chat" and segments[1] == "sessions":
          self._write_json(HTTPStatus.OK, host.list_chat_sessions())
          return
        if len(segments) == 4 and segments[0] == "chat" and segments[1] == "sessions" and segments[3] == "messages":
          self._write_json(HTTPStatus.OK, host.list_chat_messages(segments[2]))
          return
        if len(segments) == 1 and segments[0] == "approvals":
          self._write_json(HTTPStatus.OK, host.list_pending_approvals(query))
          return
        if len(segments) == 1 and segments[0] == "tool-calls":
          self._write_json(HTTPStatus.OK, host.list_tool_calls(query))
          return
        if len(segments) == 1 and segments[0] == "skills":
          self._write_json(HTTPStatus.OK, host.list_skills(query))
          return
        if len(segments) == 1 and segments[0] == "config":
          self._write_json(HTTPStatus.OK, host.list_config_sections())
          return
        if len(segments) == 2 and segments[0] == "config":
          self._write_json(HTTPStatus.OK, host.get_config_section(segments[1]))
          return
        if len(segments) == 2 and segments[0] == "delegations":
          parent_run_id = _query_required_str(query, "parent_run_id")
          wait_ms = _query_optional_int(query, "wait_ms")
          response = asyncio.run(host.get_delegation_status(parent_run_id, [segments[1]], wait_ms=wait_ms))
          self._write_json(HTTPStatus.OK, response)
          return
        if len(segments) == 1 and segments[0] == "mcp-servers":
          self._write_json(HTTPStatus.OK, host.list_mcp_servers(query))
          return
        if len(segments) == 1 and segments[0] == "scheduled-tasks":
          self._write_json(HTTPStatus.OK, host.list_scheduled_tasks(query))
          return
        if len(segments) == 3 and segments[0] == "scheduled-tasks" and segments[2] == "triggers":
          self._write_json(HTTPStatus.OK, host.get_scheduled_task_triggers(segments[1]))
          return
        if len(segments) == 2 and segments[0] == "control" and segments[1] == "health":
          self._write_json(HTTPStatus.OK, asyncio.run(host.control_health()))
          return
        if len(segments) == 2 and segments[0] == "control" and segments[1] == "targets":
          self._write_json(HTTPStatus.OK, host.control_targets(query))
          return
        self._write_json(HTTPStatus.NOT_FOUND, error_response("route not found", path=path))
      except KeyError as exc:
        self._write_json(HTTPStatus.NOT_FOUND, error_response(str(exc)))
      except (ValueError, DomainError) as exc:
        self._write_json(HTTPStatus.BAD_REQUEST, error_response(str(exc)))
      except Exception as exc:
        self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, error_response("internal server error", detail=str(exc)))

    def do_POST(self) -> None:
      path = urlparse(self.path).path
      segments = [unquote(segment) for segment in path.split("/") if segment]
      try:
        payload = self._read_json_body()
        if len(segments) == 3 and segments[0] == "runs" and segments[2] == "cancel":
          self._write_json(HTTPStatus.OK, host.cancel_run(segments[1], payload))
          return
        if len(segments) == 3 and segments[0] == "runs" and segments[2] == "interventions":
          self._write_json(HTTPStatus.OK, host.apply_intervention(segments[1], payload))
          return
        if len(segments) == 3 and segments[0] == "approvals" and segments[2] == "approve":
          self._write_json(HTTPStatus.OK, host.approve(segments[1], payload))
          return
        if len(segments) == 3 and segments[0] == "approvals" and segments[2] == "reject":
          self._write_json(HTTPStatus.OK, host.reject(segments[1]))
          return
        if len(segments) == 3 and segments[0] == "tool-calls" and segments[2] == "cancel":
          response = asyncio.run(host.cancel_tool_call(segments[1], payload))
          self._write_json(HTTPStatus.OK, response)
          return
        if len(segments) == 3 and segments[0] == "tool-calls" and segments[2] == "kill":
          response = asyncio.run(host.kill_tool_call(segments[1]))
          self._write_json(HTTPStatus.OK, response)
          return
        if len(segments) == 1 and segments[0] == "delegations":
          response = asyncio.run(host.delegate_agent(payload))
          self._write_json(HTTPStatus.CREATED, response)
          return
        if len(segments) == 2 and segments[0] == "delegations" and segments[1] == "status":
          response = asyncio.run(
            host.get_delegation_status(
              _required_str(payload.get("parent_run_id"), "parent_run_id"),
              _optional_string_list(payload.get("task_ids"), field="task_ids"),
              wait_ms=_optional_int(payload.get("wait_ms"), field="wait_ms"),
            )
          )
          self._write_json(HTTPStatus.OK, response)
          return
        if len(segments) == 3 and segments[0] == "delegations" and segments[2] == "cancel":
          response = asyncio.run(host.cancel_delegation(segments[1], payload))
          self._write_json(HTTPStatus.OK, response)
          return
        if len(segments) == 1 and segments[0] == "tasks":
          response = asyncio.run(host.create_task(payload))
          self._write_json(HTTPStatus.CREATED, response)
          return
        if len(segments) == 2 and segments[0] == "chat" and segments[1] == "sessions":
          self._write_json(HTTPStatus.CREATED, host.create_chat_session(payload))
          return
        if len(segments) == 4 and segments[0] == "chat" and segments[1] == "sessions" and segments[3] == "messages":
          response = asyncio.run(host.send_chat_message(segments[2], payload))
          self._write_json(HTTPStatus.CREATED, response)
          return
        if len(segments) == 4 and segments[0] == "chat" and segments[1] == "sessions" and segments[3] == "retry":
          response = asyncio.run(host.retry_chat_message(segments[2]))
          self._write_json(HTTPStatus.CREATED, response)
          return
        if len(segments) == 4 and segments[0] == "chat" and segments[1] == "sessions" and segments[3] == "pause":
          self._write_json(HTTPStatus.OK, host.pause_chat_session(segments[2], payload))
          return
        if len(segments) == 3 and segments[0] == "collaboration" and segments[1] == "workbenches":
          if segments[2] == "group-chat":
            self._write_json(HTTPStatus.CREATED, host.create_group_chat_workbench(payload))
            return
          if segments[2] == "cli":
            self._write_json(HTTPStatus.CREATED, asyncio.run(host.create_cli_workbench(payload)))
            return
          if segments[2] == "technical-review":
            self._write_json(HTTPStatus.CREATED, asyncio.run(host.create_technical_review_workbench(payload)))
            return
          if segments[2] == "parallel-delegation":
            self._write_json(HTTPStatus.CREATED, asyncio.run(host.create_parallel_delegation_workbench(payload)))
            return
        if len(segments) == 4 and segments[0] == "collaboration" and segments[1] == "workbenches":
          if segments[3] == "messages":
            self._write_json(HTTPStatus.CREATED, host.send_collaboration_workbench_message(segments[2], payload))
            return
          if segments[3] == "decision":
            self._write_json(HTTPStatus.CREATED, host.create_collaboration_decision_artifact(segments[2], payload))
            return
          if segments[3] == "cancel":
            self._write_json(HTTPStatus.OK, asyncio.run(host.cancel_collaboration_workbench(segments[2], payload)))
            return
        if len(segments) == 1 and segments[0] == "mcp-servers":
          self._write_json(HTTPStatus.OK, host.upsert_mcp_server(payload))
          return
        if len(segments) == 1 and segments[0] == "skills":
          self._write_json(HTTPStatus.CREATED, host.create_skill(payload))
          return
        if len(segments) == 3 and segments[0] == "skills" and segments[2] == "activate":
          self._write_json(HTTPStatus.OK, host.activate_skill(segments[1]))
          return
        if len(segments) == 3 and segments[0] == "skills" and segments[2] == "deprecate":
          self._write_json(HTTPStatus.OK, host.deprecate_skill(segments[1]))
          return
        if len(segments) == 1 and segments[0] == "scheduled-tasks":
          self._write_json(HTTPStatus.CREATED, host.create_scheduled_task(payload))
          return
        if len(segments) == 2 and segments[0] == "scheduled-tasks" and segments[1] == "run-due":
          response = asyncio.run(host.run_due_scheduled_tasks(payload))
          self._write_json(HTTPStatus.OK, response)
          return
        if len(segments) == 2 and segments[0] == "control" and segments[1] == "commands":
          response = asyncio.run(host.execute_control_command(payload))
          self._write_json(HTTPStatus.OK, response)
          return
        self._write_json(HTTPStatus.NOT_FOUND, error_response("route not found", path=path))
      except KeyError as exc:
        self._write_json(HTTPStatus.NOT_FOUND, error_response(str(exc)))
      except (ValueError, DomainError, json.JSONDecodeError) as exc:
        self._write_json(HTTPStatus.BAD_REQUEST, error_response(str(exc)))
      except Exception as exc:
        self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, error_response("internal server error", detail=str(exc)))

    def do_DELETE(self) -> None:
      path = urlparse(self.path).path
      segments = [unquote(segment) for segment in path.split("/") if segment]
      try:
        if len(segments) == 2 and segments[0] == "mcp-servers":
          self._write_json(HTTPStatus.OK, host.delete_mcp_server(segments[1]))
          return
        if len(segments) == 2 and segments[0] == "scheduled-tasks":
          self._write_json(HTTPStatus.OK, host.delete_scheduled_task(segments[1]))
          return
        if len(segments) == 4 and segments[0] == "chat" and segments[1] == "sessions" and segments[3] == "messages":
          self._write_json(HTTPStatus.OK, host.clear_chat_messages(segments[2]))
          return
        self._write_json(HTTPStatus.NOT_FOUND, error_response("route not found", path=path))
      except (ValueError, DomainError) as exc:
        self._write_json(HTTPStatus.BAD_REQUEST, error_response(str(exc)))

    def do_PATCH(self) -> None:
      path = urlparse(self.path).path
      segments = [unquote(segment) for segment in path.split("/") if segment]
      try:
        payload = self._read_json_body()
        if len(segments) == 2 and segments[0] == "scheduled-tasks":
          self._write_json(HTTPStatus.OK, host.update_scheduled_task(segments[1], payload))
          return
        if len(segments) == 2 and segments[0] == "skills":
          self._write_json(HTTPStatus.OK, host.update_skill(segments[1], payload))
          return
        if len(segments) == 2 and segments[0] == "config":
          self._write_json(HTTPStatus.OK, host.update_config_section(segments[1], payload))
          return
        self._write_json(HTTPStatus.NOT_FOUND, error_response("route not found", path=path))
      except KeyError as exc:
        self._write_json(HTTPStatus.NOT_FOUND, error_response(str(exc)))
      except (ValueError, DomainError, json.JSONDecodeError) as exc:
        self._write_json(HTTPStatus.BAD_REQUEST, error_response(str(exc)))

    def log_message(self, format: str, *args: object) -> None:
      return

    def _read_json_body(self) -> dict[str, Any]:
      length = int(self.headers.get("Content-Length") or "0")
      if length == 0:
        return {}
      raw = self.rfile.read(length)
      payload = json.loads(raw.decode("utf-8"))
      if not isinstance(payload, dict):
        raise ValueError("Request body must be a JSON object.")
      return payload

    def _write_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
      body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
      self.send_response(status.value)
      self._write_common_headers()
      self.send_header("Content-Type", "application/json; charset=utf-8")
      self.send_header("Content-Length", str(len(body)))
      self.end_headers()
      self.wfile.write(body)

    def _write_ndjson(self, status: HTTPStatus, payloads: list[dict[str, Any]]) -> None:
      body = b"".join(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
        for payload in payloads
      )
      self.send_response(status.value)
      self._write_common_headers()
      self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
      self.send_header("Cache-Control", "no-cache")
      self.send_header("Content-Length", str(len(body)))
      self.end_headers()
      self.wfile.write(body)

    def _write_sse(self, status: HTTPStatus, envelopes: list[EventStreamEnvelope]) -> None:
      body = b"".join(_sse_frame(envelope) for envelope in envelopes)
      self.send_response(status.value)
      self._write_common_headers()
      self.send_header("Content-Type", "text/event-stream; charset=utf-8")
      self.send_header("Cache-Control", "no-cache")
      self.send_header("Connection", "keep-alive")
      self.send_header("Content-Length", str(len(body)))
      self.end_headers()
      self.wfile.write(body)

    def _write_common_headers(self) -> None:
      self.send_header("Access-Control-Allow-Origin", "*")
      self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
      self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept")

    def _wants_sse(self, query: dict[str, list[str]]) -> bool:
      formats = {value.lower() for value in query.get("format", [])}
      if "sse" in formats:
        return True
      return "text/event-stream" in self.headers.get("Accept", "")

  return AgentKernelHTTPRequestHandler


class SampleWorkflowTaskLauncher:
  """Default HTTP task launcher backed by the durable runtime sample workflow."""

  def __init__(self, uow_factory) -> None:
    self._uow_factory = uow_factory

  async def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
    title = payload.get("title") or payload.get("text") or payload.get("prompt") or "HTTP task"
    if not isinstance(title, str) or not title.strip():
      raise ValueError("Task title must be a non-empty string.")
    run_id = payload.get("run_id")
    if run_id is not None and not isinstance(run_id, str):
      raise ValueError("run_id must be a string when provided.")
    input_payload = payload.get("input", {})
    if input_payload is not None and not isinstance(input_payload, dict):
      raise ValueError("input must be a JSON object when provided.")
    start = bool(payload.get("start", True))
    workflow = _sample_task_workflow()
    registry = NodeExecutorRegistry()
    registry.register("echo", lambda: FunctionNodeExecutor(_sample_task_echo_node))
    registry.register(
      "finish",
      lambda: FunctionNodeExecutor(lambda ctx: NodeResult(command=ExecutionCommand(type="finish"))),
    )
    engine = RuntimeEngine(self._uow_factory, registry)
    created = engine.create_run(
      workflow,
      input={
        "title": title,
        **(input_payload or {}),
      },
      run_id=run_id,
    )
    state = await engine.run_until_waiting(workflow, created.run_id) if start else created
    return ok_response(
      task={
        "title": title,
        "run_id": state.run_id,
        "status": state.status.value if hasattr(state.status, "value") else state.status,
      },
      run=state.to_dict(),
    )


def build_server(
  conn: sqlite3.Connection,
  host: str = "127.0.0.1",
  port: int = 0,
  *,
  control_config: dict[str, Any] | None = None,
  config_path: str | None = None,
  delegation_control: AgentDelegationControl | None = None,
  default_llm_config: LLMConfig | None = None,
) -> ThreadingHTTPServer:
  control_plane = ControlPlaneService.from_config(control_config) if control_config is not None else None
  uow_factory = unit_of_work_factory(conn)
  resolved_delegation_control = delegation_control or _build_delegation_control(config_path, uow_factory)
  http_host = HTTPHost(
    uow_factory,
    control_plane=control_plane,
    delegation_control=resolved_delegation_control,
    default_llm_config=default_llm_config,
  )
  return ThreadingHTTPServer((host, port), make_handler(http_host))


def serve(
  db_path: str,
  host: str = "127.0.0.1",
  port: int = 8080,
  *,
  control_config: dict[str, Any] | None = None,
  config_path: str | None = None,
  delegation_control: AgentDelegationControl | None = None,
  default_llm_config: LLMConfig | None = None,
) -> None:
  conn = connect_sqlite(db_path, check_same_thread=False)
  try:
    server = build_server(
      conn,
      host=host,
      port=port,
      control_config=control_config,
      config_path=config_path,
      delegation_control=delegation_control,
      default_llm_config=default_llm_config,
    )
    server.serve_forever()
  finally:
    conn.close()


def _sample_task_workflow() -> WorkflowSpec:
  return WorkflowSpec(
    workflow_id="wf_http_task",
    version="0.1.0",
    name="http-task",
    input_schema={},
    output_schema={},
    nodes=[
      NodeSpec(node_id="echo", kind="echo"),
      NodeSpec(node_id="finish", kind="finish"),
    ],
    edges=[EdgeSpec(from_node="echo", to_node="finish")],
    start_node_id="echo",
  )


def _sample_task_echo_node(ctx: NodeContext) -> NodeResult:
  return NodeResult(state_patch={"echo": ctx.input.get("title") or ctx.input.get("text")})


def _build_delegation_control(
  config_path: str | None,
  uow_factory,
) -> AgentDelegationBroker | None:
  specs = load_product_cli_connector_specs(config_path)
  if not specs:
    return None
  connectors = ProductCLIConnectorFactory().build_many(specs)
  return AgentDelegationBroker(uow_factory, connectors)


def _default_desktop_atomic_grants() -> list[CapabilityGrant]:
  expires_at = utc_now() + timedelta(days=1)
  capability_ids = [
    AtomicCapabilityIds.WORKSPACE_READ,
    AtomicCapabilityIds.WORKSPACE_WRITE,
    AtomicCapabilityIds.WORKSPACE_PATCH,
    AtomicCapabilityIds.CODE_EXECUTE,
    AtomicCapabilityIds.HTTP_REQUEST,
    AtomicCapabilityIds.BROWSER_SCAN,
    AtomicCapabilityIds.BROWSER_EXECUTE_JS,
    AtomicCapabilityIds.BROWSER_NAVIGATE,
    AtomicCapabilityIds.DESKTOP_SCREENSHOT,
    AtomicCapabilityIds.DESKTOP_DUMP_UI,
    AtomicCapabilityIds.DESKTOP_CLICK,
    AtomicCapabilityIds.DESKTOP_KEY,
    AtomicCapabilityIds.DESKTOP_TYPE_TEXT,
    AtomicCapabilityIds.MOBILE_SCREENSHOT,
    AtomicCapabilityIds.MOBILE_DUMP_UI,
    AtomicCapabilityIds.MOBILE_TAP,
    AtomicCapabilityIds.MOBILE_KEY,
    AtomicCapabilityIds.MOBILE_TYPE_TEXT,
    AtomicCapabilityIds.MEMORY_CHECKPOINT,
    AtomicCapabilityIds.MEMORY_EVOLUTION_NOTE,
    AtomicCapabilityIds.USER_INPUT_REQUEST,
    AtomicCapabilityIds.AGENT_DELEGATE,
    AtomicCapabilityIds.AGENT_DELEGATION_STATUS,
    AtomicCapabilityIds.AGENT_CANCEL_DELEGATION,
    AtomicCapabilityIds.SKILL_OPEN,
    AtomicCapabilityIds.MEMORY_SEARCH,
    AtomicCapabilityIds.MEMORY_READ,
    AtomicCapabilityIds.ARTIFACT_READ,
    AtomicCapabilityIds.EVENT_SEARCH,
    AtomicCapabilityIds.CONTEXT_COMPACT,
    AtomicCapabilityIds.CONTEXT_EXPAND,
    OrchestrationCapabilityIds.TASK_CREATE,
    OrchestrationCapabilityIds.WORKFLOW_RUN,
    OrchestrationCapabilityIds.TASK_STATUS,
    OrchestrationCapabilityIds.TASK_CANCEL,
    OrchestrationCapabilityIds.AGENT_PARALLEL_DELEGATE,
    OrchestrationCapabilityIds.WORKBENCH_CREATE,
    OrchestrationCapabilityIds.WORKBENCH_STATUS,
    OrchestrationCapabilityIds.WORKBENCH_MESSAGE,
    OrchestrationCapabilityIds.WORKBENCH_DECISION,
    OrchestrationCapabilityIds.WORKBENCH_CANCEL,
  ]
  return [
    CapabilityGrant(
      grant_id=f"desktop_grant_{capability_id}",
      capability_id=capability_id,
      expires_at=expires_at,
    )
    for capability_id in capability_ids
  ]


def _tool_call_control_response(outcome: ToolCallControlOutcome) -> dict[str, Any]:
  return ok_response(
    tool_call=outcome.tool_call.to_dict(),
    dispatched=outcome.dispatched,
    requested_status=outcome.requested_status.value,
  )


def _optional_str(value: object) -> str | None:
  if value is None:
    return None
  return str(value)


def _required_str(value: object, field: str) -> str:
  if not isinstance(value, str) or not value.strip():
    raise ValueError(f"{field} must be a non-empty string.")
  return value


def _optional_string_list(value: object, *, field: str) -> list[str] | None:
  if value is None:
    return None
  if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
    raise ValueError(f"{field} must be a list of non-empty strings.")
  return value


def _optional_int(value: object, *, field: str) -> int | None:
  if value is None:
    return None
  try:
    return int(value)
  except (TypeError, ValueError) as exc:
    raise ValueError(f"{field} must be an integer.") from exc


def _query_required_str(query: dict[str, list[str]], field: str) -> str:
  values = query.get(field) or []
  value = values[0] if values else None
  return _required_str(value, field)


def _query_optional_str(query: dict[str, list[str]], field: str) -> str | None:
  values = query.get(field) or []
  return _optional_str(values[0]) if values else None


def _query_optional_int(query: dict[str, list[str]], field: str) -> int | None:
  values = query.get(field) or []
  return _optional_int(values[0], field=field) if values else None


def _query_bool(query: dict[str, list[str]], field: str, *, default: bool) -> bool:
  values = query.get(field) or []
  if not values:
    return default
  return values[0].lower() in {"1", "true", "yes", "on"}


def _positive_int(value: object, *, default: int, field: str) -> int:
  if value is None:
    return default
  try:
    parsed = int(value)
  except (TypeError, ValueError) as exc:
    raise ValueError(f"{field} must be a positive integer.") from exc
  if parsed <= 0:
    raise ValueError(f"{field} must be a positive integer.")
  return parsed


def _positive_float(value: object, *, default: float, field: str) -> float:
  if value is None:
    return default
  try:
    parsed = float(value)
  except (TypeError, ValueError) as exc:
    raise ValueError(f"{field} must be a positive number.") from exc
  if parsed <= 0:
    raise ValueError(f"{field} must be a positive number.")
  return parsed


def _event_envelope(event: Any) -> EventStreamEnvelope:
  return EventStreamEnvelope(
    event_id=event.event_id,
    event_type=event.event_type.value if hasattr(event.event_type, "value") else str(event.event_type),
    run_id=event.run_id,
    payload={
      "node_id": event.node_id,
      "step_id": event.step_id,
      "agent_id": event.agent_id,
      "task_id": event.task_id,
      "causal_id": event.causal_id,
      "payload": event.payload,
      "artifact_refs": [ref.to_dict() for ref in event.artifact_refs],
    },
    emitted_at=event.timestamp.isoformat(),
  )


def _events_after(events: list[Any], event_id: str) -> list[Any]:
  for index, event in enumerate(events):
    if event.event_id == event_id:
      return events[index + 1 :]
  return events


def _sse_frame(envelope: EventStreamEnvelope) -> bytes:
  data = json.dumps(envelope.to_dict(), ensure_ascii=False, sort_keys=True)
  frame = f"id: {envelope.event_id}\nevent: {envelope.event_type}\ndata: {data}\n\n"
  return frame.encode("utf-8")
