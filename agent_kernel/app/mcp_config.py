"""MCP configuration management service."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from agent_kernel.capabilities.adapters.mcp import MCPServerCommand, StdioMCPClient
from agent_kernel.domain.base import utc_now
from agent_kernel.domain.mcp_config import MCPServerDefinition, MCPTransport


class MCPConfigService:
  """Stores MCP server definitions and builds runtime client commands."""

  def __init__(self, uow_factory) -> None:
    self._uow_factory = uow_factory

  def upsert_server(self, data: dict[str, Any]) -> MCPServerDefinition:
    server = self._server_from_dict(data)
    with self._uow_factory() as uow:
      existing = uow.interactions.get_mcp_server(server.name)
      if existing is not None:
        server = replace(server, created_at=existing.created_at, updated_at=utc_now())
      uow.interactions.save_mcp_server(server)
    return server

  def list_servers(
    self,
    *,
    enabled_only: bool = False,
    agent_type: str | None = None,
  ) -> list[MCPServerDefinition]:
    with self._uow_factory() as uow:
      servers = uow.interactions.list_mcp_servers()
    if enabled_only:
      servers = [server for server in servers if server.enabled]
    if agent_type is not None:
      servers = [server for server in servers if server.is_available_for_agent(agent_type)]
    return servers

  def get_server(self, name: str) -> MCPServerDefinition | None:
    with self._uow_factory() as uow:
      return uow.interactions.get_mcp_server(name)

  def delete_server(self, name: str) -> bool:
    with self._uow_factory() as uow:
      return uow.interactions.delete_mcp_server(name)

  def export_config(self, *, enabled_only: bool = False, agent_type: str | None = None) -> dict[str, Any]:
    return {"mcp_servers": [server.to_dict() for server in self.list_servers(enabled_only=enabled_only, agent_type=agent_type)]}

  def import_config(self, data: dict[str, Any]) -> list[MCPServerDefinition]:
    raw_servers = data.get("mcp_servers", [])
    if isinstance(raw_servers, dict):
      iterable = [
        {"name": name, **server}
        for name, server in raw_servers.items()
        if isinstance(server, dict)
      ]
    elif isinstance(raw_servers, list):
      iterable = raw_servers
    else:
      raise ValueError("mcp_servers must be a list or object.")
    imported: list[MCPServerDefinition] = []
    for raw in iterable:
      if not isinstance(raw, dict):
        raise ValueError("Each MCP server definition must be an object.")
      imported.append(self.upsert_server(raw))
    return imported

  def build_stdio_client(self, *, agent_type: str | None = None) -> StdioMCPClient:
    commands = {
      server.name: self.to_server_command(server)
      for server in self.list_servers(enabled_only=True, agent_type=agent_type)
      if server.transport.type == "stdio"
    }
    return StdioMCPClient(commands)

  @staticmethod
  def to_server_command(server: MCPServerDefinition) -> MCPServerCommand:
    if server.transport.type != "stdio":
      raise ValueError("Only stdio MCP servers can be converted to MCPServerCommand.")
    return MCPServerCommand(
      argv=[str(server.transport.command), *server.transport.args],
      cwd=server.transport.cwd,
      env=server.transport.env or None,
      request_timeout_seconds=server.transport.timeout_seconds,
    )

  @staticmethod
  def _server_from_dict(data: dict[str, Any]) -> MCPServerDefinition:
    transport = data.get("transport", {})
    if not isinstance(transport, dict):
      raise ValueError("MCP server transport must be an object.")
    return MCPServerDefinition(
      name=str(data.get("name") or ""),
      description=str(data["description"]) if data.get("description") is not None else None,
      enabled=bool(data.get("enabled", True)),
      agent_types=[str(item) for item in data.get("agent_types", data.get("agents", []))],
      capability_prefix=str(data["capability_prefix"]) if data.get("capability_prefix") is not None else None,
      metadata=data.get("metadata") if isinstance(data.get("metadata"), dict) else {},
      transport=MCPTransport.from_dict(transport),
    )
