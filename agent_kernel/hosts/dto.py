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
    HTTPRouteSpec(method="GET", path="/artifacts/{artifact_id}", description="Inspect artifact."),
    HTTPRouteSpec(method="POST", path="/approvals/{approval_id}/approve", description="Approve request."),
    HTTPRouteSpec(method="POST", path="/approvals/{approval_id}/reject", description="Reject request."),
    HTTPRouteSpec(method="POST", path="/tool-calls/{tool_call_id}/cancel", description="Cancel tool call."),
    HTTPRouteSpec(method="POST", path="/tool-calls/{tool_call_id}/kill", description="Kill tool call."),
    HTTPRouteSpec(method="POST", path="/delegations", description="Delegate work to an external agent connector."),
    HTTPRouteSpec(method="POST", path="/delegations/status", description="Inspect delegation status by parent run."),
    HTTPRouteSpec(method="GET", path="/delegations/{task_id}", description="Inspect one delegation task."),
    HTTPRouteSpec(method="POST", path="/delegations/{task_id}/cancel", description="Cancel a delegation task."),
    HTTPRouteSpec(method="GET", path="/mcp-servers", description="List configured MCP servers."),
    HTTPRouteSpec(method="POST", path="/mcp-servers", description="Upsert a configured MCP server."),
    HTTPRouteSpec(method="DELETE", path="/mcp-servers/{name}", description="Delete a configured MCP server."),
    HTTPRouteSpec(method="GET", path="/scheduled-tasks", description="List scheduled tasks."),
    HTTPRouteSpec(method="POST", path="/scheduled-tasks", description="Create a scheduled task."),
    HTTPRouteSpec(method="POST", path="/scheduled-tasks/run-due", description="Trigger due scheduled tasks."),
    HTTPRouteSpec(method="GET", path="/control/health", description="Inspect control workbench health."),
    HTTPRouteSpec(method="GET", path="/control/targets", description="List browser/desktop/mobile targets."),
  ]
