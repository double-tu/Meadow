"""MCP adapter protocol and stdio client."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from typing import Any, Protocol

from agent_kernel.domain.capability import ToolResult


class MCPClient(Protocol):
  async def list_tools(self, server_name: str) -> list[dict[str, Any]]:
    ...

  async def call_tool(self, server_name: str, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
    ...


class FakeMCPClient:
  def __init__(self) -> None:
    self._responses: dict[tuple[str, str], ToolResult] = {}
    self.calls: list[tuple[str, str, dict[str, Any]]] = []
    self.tools: dict[str, list[dict[str, Any]]] = {}

  def register_response(self, server_name: str, tool_name: str, result: ToolResult) -> None:
    self._responses[(server_name, tool_name)] = result

  def register_tools(self, server_name: str, tools: list[dict[str, Any]]) -> None:
    self.tools[server_name] = tools

  async def list_tools(self, server_name: str) -> list[dict[str, Any]]:
    return self.tools.get(server_name, [])

  async def call_tool(self, server_name: str, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
    self.calls.append((server_name, tool_name, arguments))
    try:
      return self._responses[(server_name, tool_name)]
    except KeyError:
      return ToolResult(
        ok=False,
        error={"type": "mcp_tool_not_found", "server": server_name, "tool": tool_name},
      )


@dataclass(slots=True)
class MCPServerCommand:
  argv: list[str]
  cwd: str | None = None
  env: dict[str, str] | None = None
  protocol_version: str = "2025-06-18"
  client_name: str = "meadow"
  client_version: str = "0.1.0"
  startup_timeout_seconds: float = 5.0
  request_timeout_seconds: float = 60.0
  shutdown_timeout_seconds: float = 1.0


@dataclass(slots=True)
class MCPToolBinding:
  server_name: str
  tool_name: str


@dataclass(slots=True)
class _MCPServerSession:
  process: asyncio.subprocess.Process
  command: MCPServerCommand
  next_request_id: int = 1
  lock: asyncio.Lock | None = None

  def __post_init__(self) -> None:
    if self.lock is None:
      self.lock = asyncio.Lock()


class StdioMCPClient:
  """Minimal newline-delimited JSON-RPC MCP stdio client."""

  def __init__(self, commands: dict[str, MCPServerCommand]) -> None:
    self._commands = commands
    self._sessions: dict[str, _MCPServerSession] = {}

  async def start(self, server_name: str) -> None:
    if server_name in self._sessions:
      return
    try:
      command = self._commands[server_name]
    except KeyError as exc:
      raise KeyError(f"MCP server command not registered: {server_name}") from exc
    process = await asyncio.create_subprocess_exec(
      *command.argv,
      cwd=command.cwd,
      env={**os.environ, **(command.env or {})},
      stdin=asyncio.subprocess.PIPE,
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.PIPE,
    )
    session = _MCPServerSession(process=process, command=command)
    self._sessions[server_name] = session
    try:
      response = await self._request(
        session,
        "initialize",
        {
          "protocolVersion": command.protocol_version,
          "capabilities": {},
          "clientInfo": {
            "name": command.client_name,
            "version": command.client_version,
          },
        },
        timeout=command.startup_timeout_seconds,
      )
      if "error" in response:
        raise RuntimeError(f"MCP initialize failed: {response['error']!r}")
      await self._notify(session, "notifications/initialized", {})
    except Exception:
      await self.stop(server_name, reason="startup failed")
      raise

  async def list_tools(self, server_name: str) -> list[dict[str, Any]]:
    session = await self._ensure_session(server_name)
    response = await self._request(
      session,
      "tools/list",
      {},
      timeout=session.command.request_timeout_seconds,
    )
    if "error" in response:
      raise RuntimeError(f"MCP tools/list failed: {response['error']!r}")
    result = response.get("result")
    if not isinstance(result, dict):
      raise RuntimeError(f"MCP tools/list returned invalid result: {response!r}")
    tools = result.get("tools", [])
    if not isinstance(tools, list) or not all(isinstance(tool, dict) for tool in tools):
      raise RuntimeError(f"MCP tools/list returned invalid tools: {response!r}")
    return tools

  async def call_tool(self, server_name: str, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
    session = await self._ensure_session(server_name)
    try:
      response = await self._request(
        session,
        "tools/call",
        {"name": tool_name, "arguments": arguments},
        timeout=session.command.request_timeout_seconds,
      )
    except RuntimeError as exc:
      return ToolResult(ok=False, error={"type": "mcp_transport_error", "message": str(exc)})
    if "error" in response:
      return ToolResult(ok=False, error={"type": "mcp_error", "error": response["error"]})
    result = response.get("result")
    if not isinstance(result, dict):
      return ToolResult(ok=False, error={"type": "invalid_mcp_response", "response": response})
    if bool(result.get("isError", False)):
      return ToolResult(ok=False, output=result, error={"type": "mcp_tool_error", "result": result})
    return ToolResult(ok=True, output=result)

  async def stop(self, server_name: str, reason: str = "shutdown") -> None:
    session = self._sessions.pop(server_name, None)
    if session is None:
      return
    process = session.process
    if process.returncode is not None:
      return
    try:
      await self._request(
        session,
        "shutdown",
        {"reason": reason},
        timeout=session.command.shutdown_timeout_seconds,
      )
    except RuntimeError:
      pass
    if process.returncode is not None:
      return
    process.terminate()
    try:
      await asyncio.wait_for(process.wait(), timeout=session.command.shutdown_timeout_seconds)
    except TimeoutError:
      process.kill()
      await process.wait()

  async def stop_all(self, reason: str = "shutdown") -> None:
    for server_name in list(self._sessions):
      await self.stop(server_name, reason=reason)

  async def _ensure_session(self, server_name: str) -> _MCPServerSession:
    await self.start(server_name)
    return self._sessions[server_name]

  async def _request(
    self,
    session: _MCPServerSession,
    method: str,
    params: dict[str, Any],
    timeout: float,
  ) -> dict[str, Any]:
    if session.lock is None:
      raise RuntimeError("MCP session lock is not configured.")
    async with session.lock:
      request_id = session.next_request_id
      session.next_request_id += 1
      await self._write_json(
        session.process,
        {
          "jsonrpc": "2.0",
          "id": request_id,
          "method": method,
          "params": params,
        },
      )
      return await self._read_response(session.process, request_id, timeout)

  async def _notify(self, session: _MCPServerSession, method: str, params: dict[str, Any]) -> None:
    if session.lock is None:
      raise RuntimeError("MCP session lock is not configured.")
    async with session.lock:
      await self._write_json(
        session.process,
        {
          "jsonrpc": "2.0",
          "method": method,
          "params": params,
        },
      )

  @staticmethod
  async def _write_json(process: asyncio.subprocess.Process, payload: dict[str, Any]) -> None:
    if process.stdin is None:
      raise RuntimeError("MCP stdin is not available.")
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if "\n" in data:
      raise RuntimeError("MCP stdio message must not contain embedded newlines.")
    process.stdin.write(data.encode("utf-8") + b"\n")
    await process.stdin.drain()

  @staticmethod
  async def _read_response(
    process: asyncio.subprocess.Process,
    request_id: int,
    timeout: float,
  ) -> dict[str, Any]:
    if process.stdout is None:
      raise RuntimeError("MCP stdout is not available.")
    while True:
      line = await asyncio.wait_for(process.stdout.readline(), timeout=timeout)
      if not line:
        stderr = ""
        if process.stderr is not None:
          stderr = (await process.stderr.read()).decode("utf-8", errors="replace")
        raise RuntimeError(f"MCP server closed stdout. stderr={stderr}")
      try:
        frame = json.loads(line.decode("utf-8"))
      except json.JSONDecodeError as exc:
        raise RuntimeError("MCP server emitted invalid JSON.") from exc
      if not isinstance(frame, dict):
        raise RuntimeError("MCP JSON-RPC frame must be an object.")
      if frame.get("id") != request_id:
        continue
      if "error" in frame:
        return frame
      if "result" not in frame:
        raise RuntimeError(f"MCP response missing result/error: {frame!r}")
      return frame


class MCPToolExecutor:
  """Maps kernel capability ids to MCP server tools."""

  def __init__(self, client: MCPClient) -> None:
    self._client = client
    self._bindings: dict[str, MCPToolBinding] = {}

  def register(self, capability_id: str, server_name: str, tool_name: str) -> None:
    self._bindings[capability_id] = MCPToolBinding(server_name=server_name, tool_name=tool_name)

  async def call(self, capability_id: str, arguments: dict[str, Any]) -> ToolResult:
    try:
      binding = self._bindings[capability_id]
    except KeyError as exc:
      raise KeyError(f"No MCP tool registered: {capability_id}") from exc
    return await self._client.call_tool(binding.server_name, binding.tool_name, arguments)
