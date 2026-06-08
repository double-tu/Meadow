"""Host DTO helpers."""

from dataclasses import dataclass, field
from typing import Any

from agent_kernel.domain.base import DomainModel, utc_now


def ok_response(**data: Any) -> dict[str, Any]:
  return {"ok": True, **data}


def error_response(message: str, **data: Any) -> dict[str, Any]:
  return {"ok": False, "error": message, **data}


@dataclass(slots=True)
class EventStreamEnvelope(DomainModel):
  event_id: str
  event_type: str
  run_id: str | None = None
  payload: dict[str, Any] = field(default_factory=dict)
  emitted_at: str = field(default_factory=lambda: utc_now().isoformat())


@dataclass(slots=True)
class TaskWorkspaceDTO(DomainModel):
  workspace_id: str
  title: str
  conversation_id: str | None = None
  task_ids: list[str] = field(default_factory=list)
  run_ids: list[str] = field(default_factory=list)
  agent_session_ids: list[str] = field(default_factory=list)
  channel_ids: list[str] = field(default_factory=list)
  artifact_ids: list[str] = field(default_factory=list)
  controls: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class HTTPRouteSpec(DomainModel):
  method: str
  path: str
  description: str
  request_schema: dict[str, Any] = field(default_factory=dict)
  response_schema: dict[str, Any] = field(default_factory=dict)


def default_http_routes() -> list[HTTPRouteSpec]:
  return [
    HTTPRouteSpec(method="POST", path="/tasks", description="Create task."),
    HTTPRouteSpec(method="GET", path="/runs/{run_id}", description="Inspect run state."),
    HTTPRouteSpec(method="POST", path="/runs/{run_id}/cancel", description="Cancel run."),
    HTTPRouteSpec(method="POST", path="/runs/{run_id}/interventions", description="Append human intervention."),
    HTTPRouteSpec(method="GET", path="/runs/{run_id}/events", description="Stream run events as NDJSON or SSE."),
    HTTPRouteSpec(method="GET", path="/events", description="Stream global or run-filtered events as NDJSON or SSE."),
    HTTPRouteSpec(method="GET", path="/artifacts/{artifact_id}", description="Inspect artifact."),
    HTTPRouteSpec(method="GET", path="/workspaces", description="List desktop workspace snapshots."),
    HTTPRouteSpec(method="GET", path="/workspaces/{workspace_id}", description="Inspect one desktop workspace snapshot."),
    HTTPRouteSpec(method="GET", path="/collaboration/workbenches", description="List collaboration workbenches."),
    HTTPRouteSpec(method="GET", path="/collaboration/workbenches/{workbench_id}", description="Inspect a collaboration workbench."),
    HTTPRouteSpec(method="POST", path="/collaboration/workbenches/group-chat", description="Create a group-chat workbench."),
    HTTPRouteSpec(method="POST", path="/collaboration/workbenches/cli", description="Create a multi-CLI collaboration workbench."),
    HTTPRouteSpec(method="POST", path="/collaboration/workbenches/technical-review", description="Create a technical review workbench."),
    HTTPRouteSpec(method="POST", path="/collaboration/workbenches/parallel-delegation", description="Create a parallel child-agent delegation workbench."),
    HTTPRouteSpec(method="POST", path="/collaboration/workbenches/{workbench_id}/messages", description="Append a workbench channel message."),
    HTTPRouteSpec(method="POST", path="/collaboration/workbenches/{workbench_id}/decision", description="Create a group-chat decision artifact."),
    HTTPRouteSpec(method="POST", path="/collaboration/workbenches/{workbench_id}/cancel", description="Cancel a collaboration workbench and child delegations."),
    HTTPRouteSpec(method="GET", path="/chat/sessions", description="List desktop chat sessions."),
    HTTPRouteSpec(method="POST", path="/chat/sessions", description="Create a desktop chat session."),
    HTTPRouteSpec(method="GET", path="/chat/sessions/{session_id}/messages", description="List desktop chat messages."),
    HTTPRouteSpec(method="POST", path="/chat/sessions/{session_id}/messages", description="Send a desktop chat message."),
    HTTPRouteSpec(method="POST", path="/chat/sessions/{session_id}/retry", description="Retry the latest user chat message."),
    HTTPRouteSpec(method="POST", path="/chat/sessions/{session_id}/pause", description="Pause/cancel the active chat run when possible."),
    HTTPRouteSpec(method="DELETE", path="/chat/sessions/{session_id}/messages", description="Clear chat context messages."),
    HTTPRouteSpec(method="GET", path="/approvals", description="List pending approvals, optionally filtered by run."),
    HTTPRouteSpec(method="POST", path="/approvals/{approval_id}/approve", description="Approve request."),
    HTTPRouteSpec(method="POST", path="/approvals/{approval_id}/reject", description="Reject request."),
    HTTPRouteSpec(method="GET", path="/tool-calls", description="List tool calls, optionally filtered by run."),
    HTTPRouteSpec(method="POST", path="/tool-calls/{tool_call_id}/cancel", description="Cancel tool call."),
    HTTPRouteSpec(method="POST", path="/tool-calls/{tool_call_id}/kill", description="Kill tool call."),
    HTTPRouteSpec(method="GET", path="/skills", description="List managed skill cards."),
    HTTPRouteSpec(method="POST", path="/skills", description="Create an interpreted skill card."),
    HTTPRouteSpec(method="PATCH", path="/skills/{skill_id}", description="Update a skill card."),
    HTTPRouteSpec(method="POST", path="/skills/{skill_id}/activate", description="Activate a skill card."),
    HTTPRouteSpec(method="POST", path="/skills/{skill_id}/deprecate", description="Deprecate a skill card."),
    HTTPRouteSpec(method="GET", path="/config", description="List hot-updatable config sections."),
    HTTPRouteSpec(method="GET", path="/config/{section}", description="Inspect one config section."),
    HTTPRouteSpec(method="PATCH", path="/config/{section}", description="Update one config section."),
    HTTPRouteSpec(method="POST", path="/delegations", description="Delegate work to an external agent connector."),
    HTTPRouteSpec(method="POST", path="/delegations/status", description="Inspect delegation status by parent run."),
    HTTPRouteSpec(method="GET", path="/delegations/{task_id}", description="Inspect one delegation task."),
    HTTPRouteSpec(method="POST", path="/delegations/{task_id}/cancel", description="Cancel a delegation task."),
    HTTPRouteSpec(method="GET", path="/mcp-servers", description="List configured MCP servers."),
    HTTPRouteSpec(method="POST", path="/mcp-servers", description="Upsert a configured MCP server."),
    HTTPRouteSpec(method="DELETE", path="/mcp-servers/{name}", description="Delete a configured MCP server."),
    HTTPRouteSpec(method="GET", path="/scheduled-tasks", description="List scheduled tasks."),
    HTTPRouteSpec(method="POST", path="/scheduled-tasks", description="Create a scheduled task."),
    HTTPRouteSpec(method="PATCH", path="/scheduled-tasks/{task_id}", description="Update or enable/disable a scheduled task."),
    HTTPRouteSpec(method="DELETE", path="/scheduled-tasks/{task_id}", description="Delete a scheduled task."),
    HTTPRouteSpec(method="GET", path="/scheduled-tasks/{task_id}/triggers", description="List scheduled task trigger history."),
    HTTPRouteSpec(method="POST", path="/scheduled-tasks/run-due", description="Trigger due scheduled tasks."),
    HTTPRouteSpec(method="GET", path="/control/health", description="Inspect control workbench health."),
    HTTPRouteSpec(method="GET", path="/control/targets", description="List browser/desktop/mobile targets."),
    HTTPRouteSpec(method="POST", path="/control/commands", description="Execute a browser/desktop/mobile control command."),
  ]
