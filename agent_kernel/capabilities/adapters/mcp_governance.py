"""MCP connection governance helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from agent_kernel.domain.base import utc_now


def build_mcp_tool_name(server_name: str, tool_name: str) -> str:
  safe_server = server_name.replace(" ", "_")
  safe_tool = tool_name.replace(" ", "_")
  return f"mcp__{safe_server}__{safe_tool}"


class MCPToolDescriptionLimiter:
  def __init__(self, max_chars: int = 2048) -> None:
    self._max_chars = max_chars

  def normalize(self, tool: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(tool)
    description = normalized.get("description")
    if isinstance(description, str) and len(description) > self._max_chars:
      normalized["description"] = description[: self._max_chars] + "\n...[truncated]"
      normalized["description_truncated"] = True
    return normalized

  def normalize_many(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [self.normalize(tool) for tool in tools]


@dataclass(slots=True)
class MCPAuthFailure:
  server_name: str
  timestamp: datetime
  reason: str | None = None


class MCPAuthFailureCache:
  def __init__(self, ttl: timedelta = timedelta(minutes=15)) -> None:
    self._ttl = ttl
    self._failures: dict[str, MCPAuthFailure] = {}

  def mark_failed(self, server_name: str, reason: str | None = None) -> None:
    self._failures[server_name] = MCPAuthFailure(server_name=server_name, timestamp=utc_now(), reason=reason)

  def needs_auth(self, server_name: str) -> bool:
    failure = self._failures.get(server_name)
    if failure is None:
      return False
    if utc_now() - failure.timestamp > self._ttl:
      self._failures.pop(server_name, None)
      return False
    return True


class MCPConnectionBatchPolicy:
  def __init__(self, local_batch_size: int = 3, remote_batch_size: int = 20) -> None:
    self.local_batch_size = local_batch_size
    self.remote_batch_size = remote_batch_size

  def batch_size(self, transport_type: str) -> int:
    return self.remote_batch_size if transport_type in {"sse", "ws", "http", "streamable-http"} else self.local_batch_size


def is_mcp_session_expired_error(error: Exception) -> bool:
  text = str(error)
  return "404" in text and ("-32001" in text or "Session" in text and "expired" in text)
