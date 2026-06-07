"""MCP server configuration domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from agent_kernel.domain.base import DomainModel, utc_now
from agent_kernel.domain.errors import DomainValidationError


MCPTransportType = Literal["stdio", "http"]


@dataclass(slots=True)
class MCPTransport(DomainModel):
  type: MCPTransportType | str
  command: str | None = None
  args: list[str] = field(default_factory=list)
  cwd: str | None = None
  env: dict[str, str] = field(default_factory=dict)
  url: str | None = None
  headers: dict[str, str] = field(default_factory=dict)
  timeout_seconds: float = 30.0

  def __post_init__(self) -> None:
    if self.type not in {"stdio", "http"}:
      raise DomainValidationError("MCP transport type must be stdio or http.")
    if self.type == "stdio" and not self.command:
      raise DomainValidationError("stdio MCP transport requires command.")
    if self.type == "http" and not self.url:
      raise DomainValidationError("http MCP transport requires url.")
    if not all(isinstance(item, str) and item for item in self.args):
      raise DomainValidationError("MCP transport args must be non-empty strings.")
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in self.env.items()):
      raise DomainValidationError("MCP transport env must be a string dictionary.")
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in self.headers.items()):
      raise DomainValidationError("MCP transport headers must be a string dictionary.")


@dataclass(slots=True)
class MCPServerDefinition(DomainModel):
  name: str
  transport: MCPTransport
  description: str | None = None
  enabled: bool = True
  agent_types: list[str] = field(default_factory=list)
  capability_prefix: str | None = None
  metadata: dict[str, Any] = field(default_factory=dict)
  created_at: datetime = field(default_factory=utc_now)
  updated_at: datetime = field(default_factory=utc_now)

  def __post_init__(self) -> None:
    if not self.name.strip():
      raise DomainValidationError("MCP server name is required.")
    if isinstance(self.transport, dict):
      self.transport = MCPTransport.from_dict(self.transport)
    if not all(isinstance(item, str) and item for item in self.agent_types):
      raise DomainValidationError("MCP server agent_types must be non-empty strings.")

  def is_available_for_agent(self, agent_type: str | None) -> bool:
    return self.enabled and (not self.agent_types or agent_type in self.agent_types)
