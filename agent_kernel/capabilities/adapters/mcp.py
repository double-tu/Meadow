"""MCP adapter protocol and fake client."""

from __future__ import annotations

from typing import Any, Protocol

from agent_kernel.domain.capability import ToolResult


class MCPClient(Protocol):
  async def call_tool(self, server_name: str, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
    ...


class FakeMCPClient:
  def __init__(self) -> None:
    self._responses: dict[tuple[str, str], ToolResult] = {}
    self.calls: list[tuple[str, str, dict[str, Any]]] = []

  def register_response(self, server_name: str, tool_name: str, result: ToolResult) -> None:
    self._responses[(server_name, tool_name)] = result

  async def call_tool(self, server_name: str, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
    self.calls.append((server_name, tool_name, arguments))
    try:
      return self._responses[(server_name, tool_name)]
    except KeyError:
      return ToolResult(
        ok=False,
        error={"type": "mcp_tool_not_found", "server": server_name, "tool": tool_name},
      )
