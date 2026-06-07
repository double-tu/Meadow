"""HTTP host for runtime inspection and control."""

from __future__ import annotations

import asyncio
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
from typing import Any, Protocol
from urllib.parse import unquote, urlparse

from agent_kernel.app.tool_call_control import ToolCallControlOutcome, ToolCallControlService
from agent_kernel.domain.errors import DomainError
from agent_kernel.domain.policy import ApprovalRequest
from agent_kernel.domain.run import RunState
from agent_kernel.domain.capability import CapabilityGrant
from agent_kernel.hosts.dto import EventStreamEnvelope, error_response, ok_response
from agent_kernel.persistence import UnitOfWork, connect_sqlite
from agent_kernel.policy import ApprovalService, HumanInterventionService, InterventionOutcome
from agent_kernel.runtime import RuntimeEngine, unit_of_work_factory
from agent_kernel.workflow import NodeExecutorRegistry


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


class HTTPHost:
  def __init__(
    self,
    uow_factory,
    *,
    run_control: RunControl | None = None,
    approval_control: ApprovalControl | None = None,
    intervention_control: InterventionControl | None = None,
    tool_call_control: ToolCallControl | None = None,
  ) -> None:
    self._uow_factory = uow_factory
    self._run_control = run_control or RuntimeEngine(uow_factory, NodeExecutorRegistry())
    self._approval_control = approval_control or ApprovalService(uow_factory)
    self._intervention_control = intervention_control or HumanInterventionService(uow_factory)
    self._tool_call_control = tool_call_control or ToolCallControlService(uow_factory)

  def inspect_run(self, run_id: str) -> dict[str, Any]:
    with self._uow_factory() as uow:
      state = uow.states.get(run_id)
    if state is None:
      raise KeyError(f"Run not found: {run_id}")
    return ok_response(run=state.to_dict())

  def list_run_events(self, run_id: str) -> list[EventStreamEnvelope]:
    with self._uow_factory() as uow:
      events = uow.events.list_by_run(run_id)
    return [
      EventStreamEnvelope(
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


def make_handler(host: HTTPHost) -> type[BaseHTTPRequestHandler]:
  class AgentKernelHTTPRequestHandler(BaseHTTPRequestHandler):
    server_version = "AgentKernelHTTP/0.1"

    def do_GET(self) -> None:
      path = urlparse(self.path).path
      segments = [unquote(segment) for segment in path.split("/") if segment]
      try:
        if len(segments) == 2 and segments[0] == "runs":
          self._write_json(HTTPStatus.OK, host.inspect_run(segments[1]))
          return
        if len(segments) == 3 and segments[0] == "runs" and segments[2] == "events":
          envelopes = host.list_run_events(segments[1])
          self._write_ndjson(HTTPStatus.OK, [envelope.to_dict() for envelope in envelopes])
          return
        if len(segments) == 2 and segments[0] == "artifacts":
          self._write_json(HTTPStatus.OK, host.inspect_artifact(segments[1]))
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
        self._write_json(HTTPStatus.NOT_FOUND, error_response("route not found", path=path))
      except KeyError as exc:
        self._write_json(HTTPStatus.NOT_FOUND, error_response(str(exc)))
      except (ValueError, DomainError, json.JSONDecodeError) as exc:
        self._write_json(HTTPStatus.BAD_REQUEST, error_response(str(exc)))
      except Exception as exc:
        self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, error_response("internal server error", detail=str(exc)))

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
      self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
      self.send_header("Cache-Control", "no-cache")
      self.send_header("Content-Length", str(len(body)))
      self.end_headers()
      self.wfile.write(body)

  return AgentKernelHTTPRequestHandler


def build_server(conn: sqlite3.Connection, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
  http_host = HTTPHost(unit_of_work_factory(conn))
  return ThreadingHTTPServer((host, port), make_handler(http_host))


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8080) -> None:
  conn = connect_sqlite(db_path)
  try:
    server = build_server(conn, host=host, port=port)
    server.serve_forever()
  finally:
    conn.close()


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
