"""Workbench adapter protocol and fake implementation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
from typing import Any, Protocol
from urllib import error as url_error
from urllib import request as url_request


@dataclass(slots=True)
class WorkbenchCommand:
  command_id: str
  kind: str
  payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class WorkbenchResult:
  ok: bool
  output: dict[str, Any] = field(default_factory=dict)
  error: dict[str, Any] | None = None


class WorkbenchClient(Protocol):
  async def execute(self, command: WorkbenchCommand) -> WorkbenchResult:
    ...


class FakeWorkbenchClient:
  def __init__(self) -> None:
    self.responses: dict[str, WorkbenchResult] = {}
    self.commands: list[WorkbenchCommand] = []

  def register_response(self, kind: str, result: WorkbenchResult) -> None:
    self.responses[kind] = result

  async def execute(self, command: WorkbenchCommand) -> WorkbenchResult:
    self.commands.append(command)
    return self.responses.get(
      command.kind,
      WorkbenchResult(ok=False, error={"type": "workbench_command_not_found", "kind": command.kind}),
    )


@dataclass(slots=True)
class HTTPWorkbenchEndpoint:
  base_url: str
  path: str = "/commands"
  timeout_seconds: float = 30.0
  headers: dict[str, str] = field(default_factory=dict)

  def url(self) -> str:
    return f"{self.base_url.rstrip('/')}/{self.path.lstrip('/')}"


class HTTPWorkbenchClient:
  """JSON HTTP implementation of the generic WorkbenchClient protocol."""

  def __init__(self, endpoint: HTTPWorkbenchEndpoint) -> None:
    self._endpoint = endpoint

  async def execute(self, command: WorkbenchCommand) -> WorkbenchResult:
    return await asyncio.to_thread(self._execute_sync, command)

  def _execute_sync(self, command: WorkbenchCommand) -> WorkbenchResult:
    body = json.dumps(
      {
        "command_id": command.command_id,
        "kind": command.kind,
        "payload": command.payload,
      },
      ensure_ascii=False,
      sort_keys=True,
    ).encode("utf-8")
    headers = {
      "Content-Type": "application/json",
      "Accept": "application/json",
      **self._endpoint.headers,
    }
    request = url_request.Request(
      self._endpoint.url(),
      data=body,
      headers=headers,
      method="POST",
    )
    try:
      with url_request.urlopen(request, timeout=self._endpoint.timeout_seconds) as response:
        payload = _read_json_response(response.read())
        status = getattr(response, "status", 200)
    except url_error.HTTPError as exc:
      return WorkbenchResult(
        ok=False,
        error={
          "type": "workbench_http_error",
          "status": exc.code,
          "body": exc.read().decode("utf-8", errors="replace"),
        },
      )
    except url_error.URLError as exc:
      return WorkbenchResult(
        ok=False,
        error={"type": "workbench_connection_error", "message": str(exc.reason)},
      )
    except TimeoutError:
      return WorkbenchResult(ok=False, error={"type": "workbench_timeout"})
    except ValueError as exc:
      return WorkbenchResult(ok=False, error={"type": "workbench_invalid_response", "message": str(exc)})
    return _result_from_response_payload(payload, status)


def _read_json_response(body: bytes) -> dict[str, Any]:
  try:
    payload = json.loads(body.decode("utf-8"))
  except json.JSONDecodeError as exc:
    raise ValueError("Workbench response must be JSON.") from exc
  if not isinstance(payload, dict):
    raise ValueError("Workbench response must be a JSON object.")
  return payload


def _result_from_response_payload(payload: dict[str, Any], status: int) -> WorkbenchResult:
  if "ok" not in payload:
    return WorkbenchResult(ok=200 <= status < 300, output=payload)
  output = payload.get("output", {})
  error = payload.get("error")
  if not isinstance(output, dict):
    output = {"value": output}
  if error is not None and not isinstance(error, dict):
    error = {"type": "workbench_error", "message": str(error)}
  return WorkbenchResult(ok=bool(payload["ok"]), output=output, error=error)
