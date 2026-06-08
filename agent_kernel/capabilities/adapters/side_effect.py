"""Interface-first adapters for governed filesystem and network side effects."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from agent_kernel.capabilities.adapters.local import LocalToolExecutor
from agent_kernel.domain.capability import ToolResult


class FileWorkspace(Protocol):
  """Boundary for file reads/writes used by governed tools."""

  def read_text(self, path: str) -> str:
    """Read a UTF-8 text file."""

  def write_text(self, path: str, content: str) -> int:
    """Write a UTF-8 text file and return bytes written."""


class LocalFileWorkspace:
  """Local filesystem implementation constrained to configured root directories."""

  def __init__(self, roots: list[str] | None = None) -> None:
    self._roots = [Path(root).expanduser().resolve(strict=False) for root in (roots or ["."])]

  def read_text(self, path: str) -> str:
    target = self._resolve(path)
    return target.read_text(encoding="utf-8")

  def write_text(self, path: str, content: str) -> int:
    target = self._resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return len(content.encode("utf-8"))

  def _resolve(self, path: str) -> Path:
    target = Path(path).expanduser().resolve(strict=False)
    if any(target == root or root in target.parents for root in self._roots):
      return target
    raise PermissionError(f"Path is outside workspace roots: {path}")


@dataclass(slots=True)
class HTTPResponse:
  status: int
  headers: dict[str, str]
  body: str
  url: str


class HTTPClient(Protocol):
  """Boundary for network access used by governed tools."""

  def request(
    self,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: str | None = None,
    timeout_seconds: float | None = None,
  ) -> HTTPResponse:
    """Execute an HTTP request."""


class UrllibHTTPClient:
  """Small standard-library HTTP client for real network access."""

  def request(
    self,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: str | None = None,
    timeout_seconds: float | None = None,
  ) -> HTTPResponse:
    data = body.encode("utf-8") if body is not None else None
    request = Request(_encode_url(url), data=data, headers=headers or {}, method=method.upper())
    with urlopen(request, timeout=timeout_seconds) as response:
      raw_body = response.read()
      charset = response.headers.get_content_charset() or "utf-8"
      return HTTPResponse(
        status=response.status,
        headers=dict(response.headers.items()),
        body=raw_body.decode(charset, errors="replace"),
        url=response.geturl(),
      )


def _encode_url(url: str) -> str:
  return quote(url, safe=":/?#[]@!$&'()*+,;=%")


class SideEffectToolProvider:
  """Registers governed file and HTTP tools into a LocalToolExecutor."""

  def __init__(
    self,
    *,
    file_workspace: FileWorkspace | None = None,
    http_client: HTTPClient | None = None,
  ) -> None:
    self._file_workspace = file_workspace
    self._http_client = http_client

  def register(
    self,
    local_tools: LocalToolExecutor,
    *,
    file_read_capability_id: str = "fs.read_text",
    file_write_capability_id: str = "fs.write_text",
    http_request_capability_id: str = "net.http_request",
  ) -> None:
    local_tools.register(file_read_capability_id, self.read_text)
    local_tools.register(file_write_capability_id, self.write_text)
    local_tools.register(http_request_capability_id, self.http_request)

  def read_text(self, input: dict[str, Any]) -> ToolResult:
    if self._file_workspace is None:
      return ToolResult.failure("adapter_not_configured", "File workspace is not configured.")
    path_error = self._required_string(input, "path")
    if isinstance(path_error, ToolResult):
      return path_error
    path = path_error
    try:
      content = self._file_workspace.read_text(path)
    except (OSError, PermissionError) as exc:
      return ToolResult.failure("filesystem_error", str(exc))
    return ToolResult.success(output={"path": path, "content": content})

  def write_text(self, input: dict[str, Any]) -> ToolResult:
    if self._file_workspace is None:
      return ToolResult.failure("adapter_not_configured", "File workspace is not configured.")
    path_error = self._required_string(input, "path")
    content_error = self._required_string(input, "content")
    if isinstance(path_error, ToolResult):
      return path_error
    if isinstance(content_error, ToolResult):
      return content_error
    path = path_error
    content = content_error
    try:
      bytes_written = self._file_workspace.write_text(path, content)
    except (OSError, PermissionError) as exc:
      return ToolResult.failure("filesystem_error", str(exc))
    return ToolResult.success(output={"path": path, "bytes_written": bytes_written})

  def http_request(self, input: dict[str, Any]) -> ToolResult:
    if self._http_client is None:
      return ToolResult.failure("adapter_not_configured", "HTTP client is not configured.")
    method = input.get("method", "GET")
    if not isinstance(method, str) or not method:
      return ToolResult.failure("invalid_input", "method must be a non-empty string.")
    url_error = self._required_string(input, "url")
    if isinstance(url_error, ToolResult):
      return url_error
    url = url_error
    headers = input.get("headers", {})
    if not isinstance(headers, dict) or not all(
      isinstance(key, str) and isinstance(value, str) for key, value in headers.items()
    ):
      return ToolResult.failure("invalid_input", "headers must be a string dictionary.")
    body = input.get("body")
    if body is not None and not isinstance(body, str):
      return ToolResult.failure("invalid_input", "body must be a string when provided.")
    timeout = input.get("timeout_seconds")
    timeout_seconds = timeout if isinstance(timeout, (int, float)) else None
    try:
      response = self._http_client.request(
        method,
        url,
        headers=headers,
        body=body,
        timeout_seconds=timeout_seconds,
      )
    except HTTPError as exc:
      return ToolResult.failure(
        "http_error",
        str(exc),
        output={"status": exc.code, "url": url},
      )
    except URLError as exc:
      return ToolResult.failure("network_error", str(exc.reason))
    except OSError as exc:
      return ToolResult.failure("network_error", str(exc))
    return ToolResult.success(
      output={
        "status": response.status,
        "headers": response.headers,
        "body": response.body,
        "url": response.url,
      }
    )

  @staticmethod
  def _required_string(input: dict[str, Any], key: str) -> str | ToolResult:
    value = input.get(key)
    if not isinstance(value, str) or not value:
      return ToolResult.failure("invalid_input", f"{key} must be a non-empty string.")
    return value
