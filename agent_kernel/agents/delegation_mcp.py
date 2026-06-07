"""MCP companion surface for agent delegation."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any, TextIO

from agent_kernel.agents.delegation import AgentDelegationBroker
from agent_kernel.domain.delegation import DelegationTaskReport


class DelegationMCPToolNames:
  DELEGATE = "delegate_to_agent"
  STATUS = "get_delegation_status"
  CANCEL = "cancel_delegation"


def delegation_mcp_tool_schemas() -> list[dict[str, Any]]:
  return [
    {
      "name": DelegationMCPToolNames.DELEGATE,
      "description": (
        "Start an asynchronous child-agent task through a Meadow connector. "
        "The child starts cold, so the task must include all required context."
      ),
      "inputSchema": {
        "type": "object",
        "required": ["agent_type", "task"],
        "properties": {
          "agent_type": {"type": "string"},
          "connector_id": {
            "type": "string",
            "description": "Optional connector id. Defaults to agent_type.",
          },
          "task": {"type": "string"},
          "parent_run_id": {"type": "string"},
          "working_dir": {"type": "string"},
          "metadata": {"type": "object"},
        },
      },
    },
    {
      "name": DelegationMCPToolNames.STATUS,
      "description": "Inspect delegated task status, optionally long-polling until any task becomes terminal.",
      "inputSchema": {
        "type": "object",
        "required": ["task_ids"],
        "properties": {
          "task_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
          "parent_run_id": {"type": "string"},
          "wait_ms": {"type": "integer", "minimum": 0},
        },
      },
    },
    {
      "name": DelegationMCPToolNames.CANCEL,
      "description": "Cancel a running delegated task by task_id.",
      "inputSchema": {
        "type": "object",
        "required": ["task_id"],
        "properties": {
          "task_id": {"type": "string"},
          "parent_run_id": {"type": "string"},
          "reason": {"type": "string"},
        },
      },
    },
  ]


class DelegationMCPServer:
  """Small JSON-RPC MCP server exposing Meadow delegation tools."""

  def __init__(
    self,
    broker: AgentDelegationBroker,
    *,
    default_parent_run_id: str | None = None,
    server_name: str = "meadow-delegation",
    server_version: str = "0.1.0",
  ) -> None:
    self._broker = broker
    self._default_parent_run_id = default_parent_run_id
    self._server_name = server_name
    self._server_version = server_version
    self._shutdown_requested = False

  @property
  def shutdown_requested(self) -> bool:
    return self._shutdown_requested

  async def handle_frame(self, frame: dict[str, Any]) -> dict[str, Any] | None:
    if "id" not in frame:
      return None
    request_id = frame["id"]
    try:
      result = await self._dispatch(str(frame.get("method") or ""), frame.get("params") or {})
      return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except Exception as exc:
      return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
          "code": -32000,
          "message": str(exc),
        },
      }

  async def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == "initialize":
      protocol_version = str(params.get("protocolVersion") or "2025-06-18")
      return {
        "protocolVersion": protocol_version,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": self._server_name, "version": self._server_version},
      }
    if method == "tools/list":
      return {"tools": delegation_mcp_tool_schemas()}
    if method == "tools/call":
      return await self._call_tool(params)
    if method == "shutdown":
      self._shutdown_requested = True
      return {}
    raise ValueError(f"Unsupported MCP method: {method}")

  async def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
      raise ValueError("MCP tool arguments must be an object.")
    if name == DelegationMCPToolNames.DELEGATE:
      return self._tool_result(await self._delegate(arguments))
    if name == DelegationMCPToolNames.STATUS:
      return self._tool_result(await self._status(arguments))
    if name == DelegationMCPToolNames.CANCEL:
      return self._tool_result(await self._cancel(arguments))
    raise ValueError(f"Unknown delegation MCP tool: {name}")

  async def _delegate(self, arguments: dict[str, Any]) -> dict[str, Any]:
    agent_type = self._required_string(arguments, "agent_type")
    task = self._required_string(arguments, "task")
    metadata = arguments.get("metadata") or {}
    if not isinstance(metadata, dict):
      raise ValueError("metadata must be an object when provided.")
    working_dir = arguments.get("working_dir")
    if isinstance(working_dir, str) and working_dir:
      metadata = {**metadata, "working_dir": working_dir}
    connector_id = arguments.get("connector_id") if isinstance(arguments.get("connector_id"), str) else agent_type
    report = await self._broker.delegate(
      parent_run_id=self._parent_run_id(arguments),
      connector_id=connector_id,
      agent_type=agent_type,
      task=task,
      metadata=metadata,
    )
    return {"task": report.to_dict()}

  async def _status(self, arguments: dict[str, Any]) -> dict[str, Any]:
    task_ids = arguments.get("task_ids")
    if not isinstance(task_ids, list) or not task_ids or not all(isinstance(task_id, str) for task_id in task_ids):
      raise ValueError("task_ids must be a non-empty string array.")
    wait_ms = arguments.get("wait_ms")
    if wait_ms is not None:
      wait_ms = int(wait_ms)
    reports = await self._broker.get_status(
      parent_run_id=self._parent_run_id(arguments),
      task_ids=task_ids,
      wait_ms=wait_ms,
    )
    return {"tasks": [report.to_dict() for report in reports]}

  async def _cancel(self, arguments: dict[str, Any]) -> dict[str, Any]:
    report = await self._broker.cancel(
      parent_run_id=self._parent_run_id(arguments),
      task_id=self._required_string(arguments, "task_id"),
      reason=str(arguments.get("reason") or "cancelled through MCP"),
    )
    return {"task": report.to_dict()}

  def _parent_run_id(self, arguments: dict[str, Any]) -> str:
    parent_run_id = arguments.get("parent_run_id") or self._default_parent_run_id
    if not isinstance(parent_run_id, str) or not parent_run_id:
      raise ValueError("parent_run_id is required.")
    return parent_run_id

  @staticmethod
  def _required_string(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
      raise ValueError(f"{key} is required.")
    return value

  @staticmethod
  def _tool_result(payload: dict[str, Any]) -> dict[str, Any]:
    return {
      "content": [
        {
          "type": "text",
          "text": json.dumps(payload, ensure_ascii=False, sort_keys=True),
        }
      ],
      "structuredContent": payload,
      "isError": False,
    }


async def run_delegation_mcp_stdio(
  server: DelegationMCPServer,
  *,
  stdin: TextIO | None = None,
  stdout: TextIO | None = None,
) -> None:
  input_stream = stdin or sys.stdin
  output_stream = stdout or sys.stdout
  while True:
    line = await asyncio.to_thread(input_stream.readline)
    if not line:
      break
    try:
      frame = json.loads(line)
      if not isinstance(frame, dict):
        raise ValueError("JSON-RPC frame must be an object.")
      response = await server.handle_frame(frame)
    except Exception as exc:
      response = {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32700, "message": str(exc)},
      }
    if response is not None:
      output_stream.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
      output_stream.flush()
    if server.shutdown_requested:
      break
