"""Extensible atomic capability pack.

This module provides a small default action vocabulary for conversational
agents. It is intentionally built on top of CapabilityRuntime boundaries
instead of becoming a second tool runtime.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import os
import sys
import tempfile
from typing import Any, Protocol

from agent_kernel.capabilities.adapters.local import LocalToolExecutor
from agent_kernel.capabilities.adapters.side_effect import FileWorkspace, HTTPClient, HTTPResponse
from agent_kernel.capabilities.registry import CapabilityRegistry
from agent_kernel.domain.base import new_id
from agent_kernel.domain.capability import CapabilitySpec, SideEffectLevel, ToolResult
from agent_kernel.domain.events import RuntimeEvent, RuntimeEventType
from agent_kernel.memory.facade import MemoryFacade


class AtomicCapabilityIds:
  WORKSPACE_READ = "atom.workspace.read"
  WORKSPACE_WRITE = "atom.workspace.write"
  WORKSPACE_PATCH = "atom.workspace.patch"
  CODE_EXECUTE = "atom.code.execute"
  HTTP_REQUEST = "atom.http.request"
  BROWSER_SCAN = "atom.browser.scan"
  BROWSER_EXECUTE_JS = "atom.browser.execute_js"
  BROWSER_NAVIGATE = "atom.browser.navigate"
  DESKTOP_SCREENSHOT = "atom.desktop.screenshot"
  DESKTOP_CLICK = "atom.desktop.click"
  DESKTOP_KEY = "atom.desktop.key"
  DESKTOP_TYPE_TEXT = "atom.desktop.type_text"
  DESKTOP_DUMP_UI = "atom.desktop.dump_ui"
  MOBILE_SCREENSHOT = "atom.mobile.screenshot"
  MOBILE_TAP = "atom.mobile.tap"
  MOBILE_KEY = "atom.mobile.key"
  MOBILE_TYPE_TEXT = "atom.mobile.type_text"
  MOBILE_DUMP_UI = "atom.mobile.dump_ui"
  MEMORY_CHECKPOINT = "atom.memory.checkpoint"
  MEMORY_EVOLUTION_NOTE = "atom.memory.evolution_note"
  USER_INPUT_REQUEST = "atom.user.input_request"


@dataclass(slots=True)
class AtomicToolCall:
  capability_id: str
  input: dict[str, Any]
  display_name: str


class AtomicToolCatalog(Protocol):
  def tool_schemas(self) -> list[dict[str, Any]]:
    ...

  def normalize_call(
    self,
    name: str,
    input: dict[str, Any],
    *,
    run_id: str,
    scope: str,
  ) -> AtomicToolCall:
    ...


class AtomicCapabilityProvider:
  """Registers default atomic capabilities and renders model-visible schemas."""

  def __init__(
    self,
    *,
    file_workspace: FileWorkspace | None = None,
    http_client: HTTPClient | None = None,
    memory: MemoryFacade | None = None,
    default_code_cwd: str | None = None,
  ) -> None:
    self._file_workspace = file_workspace
    self._http_client = http_client
    self._memory = memory
    self._default_code_cwd = default_code_cwd
    self._aliases = {
      "workspace_read": AtomicCapabilityIds.WORKSPACE_READ,
      "workspace_write": AtomicCapabilityIds.WORKSPACE_WRITE,
      "workspace_patch": AtomicCapabilityIds.WORKSPACE_PATCH,
      "code_execute": AtomicCapabilityIds.CODE_EXECUTE,
      "http_request": AtomicCapabilityIds.HTTP_REQUEST,
      "browser_scan": AtomicCapabilityIds.BROWSER_SCAN,
      "browser_execute_js": AtomicCapabilityIds.BROWSER_EXECUTE_JS,
      "browser_navigate": AtomicCapabilityIds.BROWSER_NAVIGATE,
      "desktop_screenshot": AtomicCapabilityIds.DESKTOP_SCREENSHOT,
      "desktop_click": AtomicCapabilityIds.DESKTOP_CLICK,
      "desktop_key": AtomicCapabilityIds.DESKTOP_KEY,
      "desktop_type_text": AtomicCapabilityIds.DESKTOP_TYPE_TEXT,
      "desktop_dump_ui": AtomicCapabilityIds.DESKTOP_DUMP_UI,
      "mobile_screenshot": AtomicCapabilityIds.MOBILE_SCREENSHOT,
      "mobile_tap": AtomicCapabilityIds.MOBILE_TAP,
      "mobile_key": AtomicCapabilityIds.MOBILE_KEY,
      "mobile_type_text": AtomicCapabilityIds.MOBILE_TYPE_TEXT,
      "mobile_dump_ui": AtomicCapabilityIds.MOBILE_DUMP_UI,
      "memory_checkpoint": AtomicCapabilityIds.MEMORY_CHECKPOINT,
      "memory_evolution_note": AtomicCapabilityIds.MEMORY_EVOLUTION_NOTE,
      "user_input_request": AtomicCapabilityIds.USER_INPUT_REQUEST,
    }

  def register(self, registry: CapabilityRegistry, local_tools: LocalToolExecutor) -> None:
    for spec in self.capability_specs():
      registry.register(spec)
    local_tools.register(AtomicCapabilityIds.WORKSPACE_READ, self.read_workspace)
    local_tools.register(AtomicCapabilityIds.WORKSPACE_WRITE, self.write_workspace)
    local_tools.register(AtomicCapabilityIds.WORKSPACE_PATCH, self.patch_workspace)
    local_tools.register(AtomicCapabilityIds.CODE_EXECUTE, self.execute_code)
    local_tools.register(AtomicCapabilityIds.HTTP_REQUEST, self.http_request)
    local_tools.register(AtomicCapabilityIds.MEMORY_CHECKPOINT, self.update_memory_checkpoint)
    local_tools.register(AtomicCapabilityIds.MEMORY_EVOLUTION_NOTE, self.record_memory_evolution_note)
    local_tools.register(AtomicCapabilityIds.USER_INPUT_REQUEST, self.request_user_input)

  def capability_specs(self) -> list[CapabilitySpec]:
    return [
      CapabilitySpec(
        capability_id=AtomicCapabilityIds.WORKSPACE_READ,
        name="Read workspace text",
        kind="tool",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        side_effect_level=SideEffectLevel.READ,
        required_grant="workspace.read",
      ),
      CapabilitySpec(
        capability_id=AtomicCapabilityIds.WORKSPACE_WRITE,
        name="Write workspace text",
        kind="tool",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        side_effect_level=SideEffectLevel.WRITE,
        required_grant="workspace.write",
        supports_idempotency=True,
      ),
      CapabilitySpec(
        capability_id=AtomicCapabilityIds.WORKSPACE_PATCH,
        name="Patch workspace text",
        kind="tool",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        side_effect_level=SideEffectLevel.WRITE,
        required_grant="workspace.write",
        supports_idempotency=True,
      ),
      CapabilitySpec(
        capability_id=AtomicCapabilityIds.CODE_EXECUTE,
        name="Execute short code",
        kind="tool",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        side_effect_level=SideEffectLevel.EXEC,
      ),
      CapabilitySpec(
        capability_id=AtomicCapabilityIds.HTTP_REQUEST,
        name="HTTP request",
        kind="tool",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        side_effect_level=SideEffectLevel.NETWORK,
        required_grant="network.http",
      ),
      CapabilitySpec(
        capability_id=AtomicCapabilityIds.BROWSER_SCAN,
        name="Inspect browser",
        kind="workbench",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        side_effect_level=SideEffectLevel.READ,
      ),
      CapabilitySpec(
        capability_id=AtomicCapabilityIds.BROWSER_EXECUTE_JS,
        name="Execute browser JavaScript",
        kind="workbench",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        side_effect_level=SideEffectLevel.EXTERNAL_MUTATION,
      ),
      CapabilitySpec(
        capability_id=AtomicCapabilityIds.BROWSER_NAVIGATE,
        name="Navigate browser",
        kind="workbench",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        side_effect_level=SideEffectLevel.EXTERNAL_MUTATION,
      ),
      *[
        CapabilitySpec(
          capability_id=capability_id,
          name=name,
          kind="workbench",
          input_schema={"type": "object"},
          output_schema={"type": "object"},
          side_effect_level=side_effect_level,
        )
        for capability_id, name, side_effect_level in [
          (AtomicCapabilityIds.DESKTOP_SCREENSHOT, "Desktop screenshot", SideEffectLevel.READ),
          (AtomicCapabilityIds.DESKTOP_DUMP_UI, "Desktop UI dump", SideEffectLevel.READ),
          (AtomicCapabilityIds.DESKTOP_CLICK, "Desktop click", SideEffectLevel.EXTERNAL_MUTATION),
          (AtomicCapabilityIds.DESKTOP_KEY, "Desktop key", SideEffectLevel.EXTERNAL_MUTATION),
          (AtomicCapabilityIds.DESKTOP_TYPE_TEXT, "Desktop type text", SideEffectLevel.EXTERNAL_MUTATION),
          (AtomicCapabilityIds.MOBILE_SCREENSHOT, "Mobile screenshot", SideEffectLevel.READ),
          (AtomicCapabilityIds.MOBILE_DUMP_UI, "Mobile UI dump", SideEffectLevel.READ),
          (AtomicCapabilityIds.MOBILE_TAP, "Mobile tap", SideEffectLevel.EXTERNAL_MUTATION),
          (AtomicCapabilityIds.MOBILE_KEY, "Mobile key", SideEffectLevel.EXTERNAL_MUTATION),
          (AtomicCapabilityIds.MOBILE_TYPE_TEXT, "Mobile type text", SideEffectLevel.EXTERNAL_MUTATION),
        ]
      ],
      CapabilitySpec(
        capability_id=AtomicCapabilityIds.MEMORY_CHECKPOINT,
        name="Update working checkpoint",
        kind="tool",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        side_effect_level=SideEffectLevel.WRITE,
      ),
      CapabilitySpec(
        capability_id=AtomicCapabilityIds.MEMORY_EVOLUTION_NOTE,
        name="Record memory evolution candidate",
        kind="tool",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        side_effect_level=SideEffectLevel.WRITE,
      ),
      CapabilitySpec(
        capability_id=AtomicCapabilityIds.USER_INPUT_REQUEST,
        name="Request user input",
        kind="tool",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        side_effect_level=SideEffectLevel.NONE,
      ),
    ]

  def tool_schemas(self) -> list[dict[str, Any]]:
    return [
      self._schema("workspace_read", "Read a UTF-8 text file from the granted workspace.", ["path"]),
      self._schema(
        "workspace_write",
        "Create, overwrite, append, or prepend a UTF-8 text file in the granted workspace.",
        ["path", "content"],
        {"mode": {"type": "string", "enum": ["overwrite", "append", "prepend"]}},
      ),
      self._schema(
        "workspace_patch",
        "Replace exactly one old_text occurrence with new_text in a workspace text file.",
        ["path", "old_text", "new_text"],
      ),
      self._schema("code_execute", "Execute short Python or shell code with timeout.", ["code"]),
      self._schema(
        "http_request",
        "Make a governed HTTP request through the configured network adapter.",
        ["url"],
        {
          "method": {"type": "string"},
          "headers": {"type": "object"},
          "body": {"type": "string"},
          "timeout_seconds": {"type": "number"},
        },
      ),
      self._schema("browser_scan", "Inspect browser targets or current page through a control workbench.", []),
      self._schema("browser_execute_js", "Execute JavaScript in a browser target.", ["code"]),
      self._schema("browser_navigate", "Navigate a browser target to a URL.", ["url"]),
      self._schema("desktop_screenshot", "Capture a desktop target screenshot.", []),
      self._schema("desktop_click", "Click desktop coordinates.", ["x", "y"]),
      self._schema("desktop_key", "Send a desktop key or shortcut.", ["key"]),
      self._schema("desktop_type_text", "Type text into a desktop target.", ["text"]),
      self._schema("desktop_dump_ui", "Dump normalized desktop UI nodes.", []),
      self._schema("mobile_screenshot", "Capture a mobile target screenshot.", []),
      self._schema("mobile_tap", "Tap mobile coordinates.", ["x", "y"]),
      self._schema("mobile_key", "Send a mobile key event.", ["key"]),
      self._schema("mobile_type_text", "Type text into a mobile target.", ["text"]),
      self._schema("mobile_dump_ui", "Dump normalized mobile UI nodes.", []),
      self._schema("memory_checkpoint", "Persist compact working context for the current task.", ["key_info"]),
      self._schema(
        "memory_evolution_note",
        "Record a candidate fact, lesson, or skill for later memory evolution.",
        ["note"],
      ),
      self._schema("user_input_request", "Pause the loop and request user input.", ["question"]),
    ]

  def normalize_call(
    self,
    name: str,
    input: dict[str, Any],
    *,
    run_id: str,
    scope: str,
  ) -> AtomicToolCall:
    capability_id = self._aliases.get(name, name)
    normalized = dict(input)
    normalized.setdefault("run_id", run_id)
    normalized.setdefault("scope", scope)
    if capability_id == AtomicCapabilityIds.WORKSPACE_PATCH:
      if "old_text" not in normalized and "old_content" in normalized:
        normalized["old_text"] = normalized["old_content"]
      if "new_text" not in normalized and "new_content" in normalized:
        normalized["new_text"] = normalized["new_content"]
    if capability_id == AtomicCapabilityIds.BROWSER_SCAN:
      normalized = {
        "action": "inspect_browser",
        "target_kind": "browser",
        "target_id": normalized.get("target_id"),
        "payload": {key: value for key, value in normalized.items() if key not in {"run_id", "scope", "target_id"}},
      }
    elif capability_id == AtomicCapabilityIds.BROWSER_EXECUTE_JS:
      code = normalized.get("code", normalized.get("script"))
      normalized = {
        "action": "execute_js",
        "target_kind": "browser",
        "target_id": normalized.get("target_id"),
        "payload": {"code": code},
      }
    elif capability_id == AtomicCapabilityIds.BROWSER_NAVIGATE:
      normalized = {
        "action": "navigate",
        "target_kind": "browser",
        "target_id": normalized.get("target_id"),
        "payload": {"url": normalized.get("url")},
      }
    elif capability_id in {
      AtomicCapabilityIds.DESKTOP_SCREENSHOT,
      AtomicCapabilityIds.DESKTOP_CLICK,
      AtomicCapabilityIds.DESKTOP_KEY,
      AtomicCapabilityIds.DESKTOP_TYPE_TEXT,
      AtomicCapabilityIds.DESKTOP_DUMP_UI,
    }:
      normalized = self._control_input("desktop", capability_id, normalized)
    elif capability_id in {
      AtomicCapabilityIds.MOBILE_SCREENSHOT,
      AtomicCapabilityIds.MOBILE_TAP,
      AtomicCapabilityIds.MOBILE_KEY,
      AtomicCapabilityIds.MOBILE_TYPE_TEXT,
      AtomicCapabilityIds.MOBILE_DUMP_UI,
    }:
      normalized = self._control_input("mobile", capability_id, normalized)
    return AtomicToolCall(capability_id=capability_id, input=normalized, display_name=name)

  def read_workspace(self, input: dict[str, Any]) -> ToolResult:
    if self._file_workspace is None:
      return ToolResult.failure("adapter_not_configured", "File workspace is not configured.")
    path = self._required_string(input, "path")
    if isinstance(path, ToolResult):
      return path
    try:
      content = self._file_workspace.read_text(path)
      view = self._select_text_view(
        content,
        start=input.get("start"),
        count=input.get("count"),
        keyword=input.get("keyword"),
        show_line_numbers=input.get("show_line_numbers", input.get("show_linenos", True)),
      )
      return ToolResult.success({"path": path, **view})
    except (OSError, PermissionError) as exc:
      return ToolResult.failure("filesystem_error", str(exc))

  def write_workspace(self, input: dict[str, Any]) -> ToolResult:
    if self._file_workspace is None:
      return ToolResult.failure("adapter_not_configured", "File workspace is not configured.")
    path = self._required_string(input, "path")
    content = self._required_string(input, "content")
    if isinstance(path, ToolResult):
      return path
    if isinstance(content, ToolResult):
      return content
    mode = input.get("mode", "overwrite")
    if mode not in {"overwrite", "append", "prepend"}:
      return ToolResult.failure("invalid_input", "mode must be overwrite, append, or prepend.")
    try:
      current = ""
      if mode in {"append", "prepend"}:
        try:
          current = self._file_workspace.read_text(path)
        except FileNotFoundError:
          current = ""
      final = content if mode == "overwrite" else current + content if mode == "append" else content + current
      bytes_written = self._file_workspace.write_text(path, final)
      return ToolResult.success({"path": path, "mode": mode, "bytes_written": bytes_written})
    except (OSError, PermissionError) as exc:
      return ToolResult.failure("filesystem_error", str(exc))

  def patch_workspace(self, input: dict[str, Any]) -> ToolResult:
    if self._file_workspace is None:
      return ToolResult.failure("adapter_not_configured", "File workspace is not configured.")
    path = self._required_string(input, "path")
    old_text = self._required_string(input, "old_text")
    new_text = self._required_string(input, "new_text")
    for value in (path, old_text, new_text):
      if isinstance(value, ToolResult):
        return value
    try:
      content = self._file_workspace.read_text(path)
      count = content.count(old_text)
      if count == 0:
        return ToolResult.failure("patch_not_found", "old_text was not found.")
      if count > 1:
        return ToolResult.failure("patch_not_unique", f"old_text matched {count} locations.")
      updated = content.replace(old_text, new_text)
      self._file_workspace.write_text(path, updated)
      return ToolResult.success({"path": path, "replacements": 1})
    except (OSError, PermissionError) as exc:
      return ToolResult.failure("filesystem_error", str(exc))

  async def execute_code(self, input: dict[str, Any]) -> ToolResult:
    code = self._required_string(input, "code")
    if isinstance(code, ToolResult):
      return code
    language = input.get("language", input.get("type", "python"))
    timeout = input.get("timeout_seconds", input.get("timeout", 30))
    timeout_seconds = float(timeout) if isinstance(timeout, (int, float)) else 30.0
    cwd = str(input.get("cwd") or self._default_code_cwd or os.getcwd())
    try:
      argv, cleanup_path = self._code_argv(str(language), code, cwd)
      process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
      )
      stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except TimeoutError:
      process.kill()
      await process.wait()
      return ToolResult.failure("timeout", f"Code execution timed out after {timeout_seconds} seconds.")
    except (OSError, ValueError) as exc:
      return ToolResult.failure("execution_error", str(exc))
    finally:
      if "cleanup_path" in locals() and cleanup_path is not None:
        Path(cleanup_path).unlink(missing_ok=True)
    return ToolResult(
      ok=process.returncode == 0,
      output={
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
        "returncode": process.returncode,
      },
      error=None
      if process.returncode == 0
      else {
        "type": "execution_failed",
        "returncode": process.returncode,
        "stderr": stderr.decode("utf-8", errors="replace"),
      },
    )

  def http_request(self, input: dict[str, Any]) -> ToolResult:
    if self._http_client is None:
      return ToolResult.failure("adapter_not_configured", "HTTP client is not configured.")
    url = self._required_string(input, "url")
    if isinstance(url, ToolResult):
      return url
    method = input.get("method", "GET")
    if not isinstance(method, str) or not method:
      return ToolResult.failure("invalid_input", "method must be a non-empty string.")
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
    except OSError as exc:
      return ToolResult.failure("network_error", str(exc))
    return self._http_response_result(response)

  def update_memory_checkpoint(self, input: dict[str, Any]) -> ToolResult:
    if self._memory is None:
      return ToolResult.failure("memory_not_configured", "Memory facade is not configured.")
    scope = str(input.get("scope") or input.get("run_id") or "default")
    key_info = self._required_string(input, "key_info")
    if isinstance(key_info, ToolResult):
      return key_info
    item = self._memory.write_working(
      scope,
      {
        "kind": "working_checkpoint",
        "key_info": key_info,
        "related": input.get("related") or input.get("related_sop"),
      },
      importance=0.9,
      created_by="atomic_capability",
    )
    return ToolResult.success({"memory_id": item.memory_id, "scope": scope})

  def record_memory_evolution_note(self, input: dict[str, Any]) -> ToolResult:
    note = self._required_string(input, "note")
    if isinstance(note, ToolResult):
      return note
    run_id = str(input.get("run_id") or "unknown_run")
    event = RuntimeEvent(
      event_type=RuntimeEventType.MEMORY_EVOLUTION_CANDIDATE,
      run_id=run_id,
      payload={
        "candidate_id": new_id("memory_candidate"),
        "scope": input.get("scope"),
        "note": note,
        "source": "atomic_capability",
        "status": "pending",
      },
    )
    return ToolResult.success({"recorded": True, "event_id": event.event_id}, events=[event])

  def request_user_input(self, input: dict[str, Any]) -> ToolResult:
    question = self._required_string(input, "question")
    if isinstance(question, ToolResult):
      return question
    candidates = input.get("candidates", [])
    if not isinstance(candidates, list):
      return ToolResult.failure("invalid_input", "candidates must be a list when provided.")
    return ToolResult.success(
      {
        "status": "needs_user_input",
        "question": question,
        "candidates": [str(candidate) for candidate in candidates],
      },
      metadata={"interrupt": "user_input"},
    )

  @classmethod
  def _control_input(cls, target_kind: str, capability_id: str, input: dict[str, Any]) -> dict[str, Any]:
    action = cls._control_action(capability_id)
    payload = {
      key: value
      for key, value in input.items()
      if key not in {"run_id", "scope", "target_id", "timeout_seconds"}
    }
    normalized: dict[str, Any] = {
      "action": action,
      "target_kind": target_kind,
      "target_id": input.get("target_id"),
      "payload": payload,
    }
    if "timeout_seconds" in input:
      normalized["timeout_seconds"] = input["timeout_seconds"]
    return normalized

  @staticmethod
  def _control_action(capability_id: str) -> str:
    return {
      AtomicCapabilityIds.DESKTOP_SCREENSHOT: "screenshot",
      AtomicCapabilityIds.DESKTOP_CLICK: "click",
      AtomicCapabilityIds.DESKTOP_KEY: "key",
      AtomicCapabilityIds.DESKTOP_TYPE_TEXT: "type_text",
      AtomicCapabilityIds.DESKTOP_DUMP_UI: "dump_ui",
      AtomicCapabilityIds.MOBILE_SCREENSHOT: "screenshot",
      AtomicCapabilityIds.MOBILE_TAP: "tap",
      AtomicCapabilityIds.MOBILE_KEY: "key",
      AtomicCapabilityIds.MOBILE_TYPE_TEXT: "type_text",
      AtomicCapabilityIds.MOBILE_DUMP_UI: "dump_ui",
    }[capability_id]

  @staticmethod
  def _http_response_result(response: HTTPResponse) -> ToolResult:
    return ToolResult.success(
      {
        "status": response.status,
        "headers": response.headers,
        "body": response.body,
        "url": response.url,
      }
    )

  @staticmethod
  def _select_text_view(
    content: str,
    *,
    start: Any = None,
    count: Any = None,
    keyword: Any = None,
    show_line_numbers: Any = True,
  ) -> dict[str, Any]:
    lines = content.splitlines()
    start_line = start if isinstance(start, int) and start > 0 else 1
    max_count = count if isinstance(count, int) and count > 0 else 200
    selected_start = start_line
    selected_lines = lines[start_line - 1 :]
    if isinstance(keyword, str) and keyword:
      lowered = keyword.lower()
      match_index = next((index for index, line in enumerate(selected_lines) if lowered in line.lower()), None)
      if match_index is None:
        return {
          "content": "",
          "start": start_line,
          "end": start_line - 1,
          "total_lines": len(lines),
          "keyword": keyword,
          "matched": False,
        }
      context_before = min(max(1, max_count // 3), match_index) if max_count > 1 else 0
      selected_start = start_line + match_index - context_before
      selected_lines = lines[selected_start - 1 :]
    clipped = selected_lines[:max_count]
    rendered = "\n".join(
      f"{selected_start + offset}|{line}" for offset, line in enumerate(clipped)
    ) if show_line_numbers else "\n".join(clipped)
    return {
      "content": rendered,
      "start": selected_start,
      "end": selected_start + len(clipped) - 1,
      "total_lines": len(lines),
      "truncated": len(selected_lines) > len(clipped),
      "matched": True if keyword else None,
    }

  @staticmethod
  def _required_string(input: dict[str, Any], key: str) -> str | ToolResult:
    value = input.get(key)
    if not isinstance(value, str) or not value:
      return ToolResult.failure("invalid_input", f"{key} must be a non-empty string.")
    return value

  @staticmethod
  def _code_argv(language: str, code: str, cwd: str) -> tuple[list[str], str | None]:
    normalized = language.lower()
    if normalized in {"python", "py"}:
      handle = tempfile.NamedTemporaryFile(
        suffix=".agent-kernel.py",
        delete=False,
        mode="w",
        encoding="utf-8",
        dir=cwd if os.path.isdir(cwd) else None,
      )
      with handle:
        handle.write(code)
      return [sys.executable, "-X", "utf8", "-u", handle.name], handle.name
    if normalized in {"shell", "sh", "bash"}:
      shell = "/bin/bash" if Path("/bin/bash").exists() else "/bin/sh"
      return [shell, "-c", code], None
    raise ValueError(f"Unsupported code language: {language}")

  @staticmethod
  def _schema(
    name: str,
    description: str,
    required: list[str],
    extra_properties: dict[str, Any] | None = None,
  ) -> dict[str, Any]:
    properties: dict[str, Any] = {
      "path": {"type": "string"},
      "content": {"type": "string"},
      "old_text": {"type": "string"},
      "new_text": {"type": "string"},
      "code": {"type": "string"},
      "url": {"type": "string"},
      "method": {"type": "string"},
      "headers": {"type": "object"},
      "body": {"type": "string"},
      "x": {"type": "integer"},
      "y": {"type": "integer"},
      "key": {"type": "string"},
      "text": {"type": "string"},
      "timeout_seconds": {"type": "number"},
      "key_info": {"type": "string"},
      "note": {"type": "string"},
      "question": {"type": "string"},
      "candidates": {"type": "array", "items": {"type": "string"}},
      "target_id": {"type": "string"},
      "scope": {"type": "string"},
    }
    properties.update(extra_properties or {})
    return {
      "type": "function",
      "function": {
        "name": name,
        "description": description,
        "parameters": {
          "type": "object",
          "properties": properties,
          "required": required,
        },
      },
    }
