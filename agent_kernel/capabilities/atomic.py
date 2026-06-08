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
import re
import sys
import tempfile
from typing import Any, Protocol

from agent_kernel.capabilities.adapters.local import LocalToolExecutor
from agent_kernel.capabilities.adapters.side_effect import FileWorkspace, HTTPClient, HTTPResponse
from agent_kernel.capabilities.registry import CapabilityRegistry
from agent_kernel.domain.base import new_id
from agent_kernel.domain.capability import CapabilitySpec, SideEffectLevel, ToolResult
from agent_kernel.domain.events import RuntimeEvent, RuntimeEventType
from agent_kernel.domain.identifiers import ArtifactRef
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
  AGENT_DELEGATE = "atom.agent.delegate"
  AGENT_DELEGATION_STATUS = "atom.agent.delegation_status"
  AGENT_CANCEL_DELEGATION = "atom.agent.cancel_delegation"
  SKILL_OPEN = "atom.context.skill_open"
  MEMORY_SEARCH = "atom.context.memory_search"
  MEMORY_READ = "atom.context.memory_read"
  ARTIFACT_READ = "atom.context.artifact_read"
  EVENT_SEARCH = "atom.context.event_search"
  CONTEXT_COMPACT = "atom.context.compact"
  CONTEXT_EXPAND = "atom.context.expand"


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


class AgentDelegationTool(Protocol):
  async def delegate(
    self,
    *,
    parent_run_id: str,
    task: str,
    connector_id: str,
    agent_type: str | None = None,
    parent_agent_id: str | None = None,
    parent_session_id: str | None = None,
    metadata: dict[str, Any] | None = None,
  ):
    ...

  async def get_status(
    self,
    *,
    parent_run_id: str,
    task_ids: list[str] | None = None,
    wait_ms: int | None = None,
  ):
    ...

  async def cancel(
    self,
    *,
    parent_run_id: str,
    task_id: str,
    reason: str = "delegation cancelled",
  ):
    ...


class SkillReader(Protocol):
  def get(self, skill_id: str):
    ...


def _summarize_http_body(body: str) -> dict[str, Any]:
  title_match = re.search(r"<title[^>]*>(.*?)</title>", body, re.IGNORECASE | re.DOTALL)
  page_title = _clean_html_text(title_match.group(1)) if title_match else None
  feed_titles = _unique_preserve_order(
    _clean_json_text(match)
    for match in re.findall(r'"displayTitle"\s*:\s*"([^"]+)"', body)
  )
  if not feed_titles:
    feed_titles = _unique_preserve_order(
      _clean_json_text(match)
      for match in re.findall(r'"title"\s*:\s*"([^"]{2,120})"', body)
    )
  summary: dict[str, Any] = {
    "page_title": page_title,
    "body_bytes": len(body.encode("utf-8")),
  }
  if feed_titles:
    summary["feed_titles"] = feed_titles[:20]
    summary["feed_count"] = len(feed_titles)
  return summary


def _clean_html_text(value: str) -> str:
  return re.sub(r"\s+", " ", value).strip()


def _clean_json_text(value: str) -> str:
  decoded = value
  if "\\" in value:
    try:
      decoded = bytes(value, "utf-8").decode("unicode_escape")
    except UnicodeDecodeError:
      decoded = value
  return re.sub(r"\s+", " ", decoded).strip()


def _unique_preserve_order(values) -> list[str]:
  seen: set[str] = set()
  result: list[str] = []
  for value in values:
    if not value or value in seen:
      continue
    seen.add(value)
    result.append(value)
  return result


def _detect_anti_bot(url: str, body: str, body_summary: dict[str, Any]) -> dict[str, Any] | None:
  haystack = f"{url}\n{body_summary.get('page_title') or ''}\n{body[:2000]}".lower()
  markers = {
    "antispider": "anti_spider_challenge",
    "captcha": "captcha_or_challenge",
    "验证码": "captcha_or_challenge",
    "安全验证": "captcha_or_challenge",
    "verify you are human": "captcha_or_challenge",
    "unusual traffic": "anti_bot_rate_limit",
  }
  for marker, issue_type in markers.items():
    if marker.lower() in haystack:
      return {
        "type": issue_type,
        "message": "The HTTP response appears to be an anti-bot or verification page, not usable search content.",
      }
  return None


def _positive_int(value: object, *, default: int, maximum: int) -> int:
  try:
    parsed = int(value) if value is not None else default
  except (TypeError, ValueError):
    parsed = default
  return max(1, min(maximum, parsed))


def _string_list(value: object) -> list[str]:
  if value is None:
    return []
  if not isinstance(value, list):
    return []
  return [str(item) for item in value if isinstance(item, str) and item]


def _memory_score(memory, query: str) -> float:
  importance = memory.importance if memory.importance is not None else 0.0
  if not query:
    return importance
  haystack = str(memory.content).lower()
  terms = [term for term in re.split(r"\s+", query.lower()) if term]
  if not terms:
    return importance
  matched = sum(1 for term in terms if term in haystack)
  return matched / len(terms) + importance * 0.2


def _memory_summary(memory, *, score: float) -> dict[str, Any]:
  content = memory.content
  summary = content.get("summary") or content.get("key_info") or content.get("outcome") or str(content)
  return {
    "memory_id": memory.memory_id,
    "memory_type": memory.memory_type,
    "scope": memory.scope,
    "score": round(score, 4),
    "importance": memory.importance,
    "summary": _compact_text(str(summary), 600),
    "artifact_refs": [ref.to_dict() for ref in memory.source_artifact_refs],
  }


def _memory_detail(memory) -> dict[str, Any]:
  data = memory.to_dict()
  data["content"] = _compact_value(data.get("content"), max_chars=2500)
  return data


def _event_summary(event: RuntimeEvent) -> dict[str, Any]:
  return {
    "event_id": event.event_id,
    "run_id": event.run_id,
    "event_type": event.event_type.value,
    "timestamp": event.timestamp.isoformat(),
    "agent_id": event.agent_id,
    "task_id": event.task_id,
    "payload": _compact_value(event.payload, max_chars=1200),
    "artifact_refs": [ref.to_dict() for ref in event.artifact_refs],
  }


def _event_detail(event: RuntimeEvent) -> dict[str, Any]:
  data = event.to_dict()
  data["payload"] = _compact_value(data.get("payload"), max_chars=2500)
  return data


def _compact_value(value: object, *, max_chars: int) -> object:
  text = str(value)
  if len(text) <= max_chars:
    return value
  return {
    "_truncated": True,
    "preview": _compact_text(text, max_chars),
    "original_chars": len(text),
  }


def _compact_text(value: str, max_chars: int) -> str:
  return value if len(value) <= max_chars else value[:max_chars] + "...[truncated]"


def _inline_artifact_content(metadata: dict[str, Any]) -> str | None:
  for key in ("content", "body", "text", "payload"):
    value = metadata.get(key)
    if isinstance(value, str):
      return value
    if value is not None and key == "payload":
      return str(value)
  return None


def _artifact_file_path(uri: str) -> str | None:
  if uri.startswith("file://"):
    return uri.removeprefix("file://")
  if uri.startswith("/") or uri.startswith("./") or uri.startswith("../"):
    return uri
  return None


class AtomicCapabilityProvider:
  """Registers default atomic capabilities and renders model-visible schemas."""

  def __init__(
    self,
    *,
    file_workspace: FileWorkspace | None = None,
    http_client: HTTPClient | None = None,
    memory: MemoryFacade | None = None,
    delegation: AgentDelegationTool | None = None,
    uow_factory: Any | None = None,
    skill_service: SkillReader | None = None,
    default_code_cwd: str | None = None,
  ) -> None:
    self._file_workspace = file_workspace
    self._http_client = http_client
    self._memory = memory
    self._delegation = delegation
    self._uow_factory = uow_factory
    self._skill_service = skill_service
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
      "agent_delegate": AtomicCapabilityIds.AGENT_DELEGATE,
      "agent_delegation_status": AtomicCapabilityIds.AGENT_DELEGATION_STATUS,
      "agent_cancel_delegation": AtomicCapabilityIds.AGENT_CANCEL_DELEGATION,
      "skill_open": AtomicCapabilityIds.SKILL_OPEN,
      "memory_search": AtomicCapabilityIds.MEMORY_SEARCH,
      "memory_read": AtomicCapabilityIds.MEMORY_READ,
      "artifact_read": AtomicCapabilityIds.ARTIFACT_READ,
      "event_search": AtomicCapabilityIds.EVENT_SEARCH,
      "context_compact": AtomicCapabilityIds.CONTEXT_COMPACT,
      "context_expand": AtomicCapabilityIds.CONTEXT_EXPAND,
    }

  def can_handle(self, name: str) -> bool:
    return name in self._aliases or name in self._aliases.values()

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
    local_tools.register(AtomicCapabilityIds.AGENT_DELEGATE, self.delegate_agent)
    local_tools.register(AtomicCapabilityIds.AGENT_DELEGATION_STATUS, self.get_delegation_status)
    local_tools.register(AtomicCapabilityIds.AGENT_CANCEL_DELEGATION, self.cancel_delegation)
    local_tools.register(AtomicCapabilityIds.SKILL_OPEN, self.open_skill)
    local_tools.register(AtomicCapabilityIds.MEMORY_SEARCH, self.search_memory)
    local_tools.register(AtomicCapabilityIds.MEMORY_READ, self.read_memory)
    local_tools.register(AtomicCapabilityIds.ARTIFACT_READ, self.read_artifact)
    local_tools.register(AtomicCapabilityIds.EVENT_SEARCH, self.search_events)
    local_tools.register(AtomicCapabilityIds.CONTEXT_COMPACT, self.compact_context)
    local_tools.register(AtomicCapabilityIds.CONTEXT_EXPAND, self.expand_context)

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
      CapabilitySpec(
        capability_id=AtomicCapabilityIds.AGENT_DELEGATE,
        name="Delegate work to agent connector",
        kind="tool",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        side_effect_level=SideEffectLevel.EXTERNAL_MUTATION,
      ),
      CapabilitySpec(
        capability_id=AtomicCapabilityIds.AGENT_DELEGATION_STATUS,
        name="Inspect agent delegation status",
        kind="tool",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        side_effect_level=SideEffectLevel.READ,
      ),
      CapabilitySpec(
        capability_id=AtomicCapabilityIds.AGENT_CANCEL_DELEGATION,
        name="Cancel agent delegation",
        kind="tool",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        side_effect_level=SideEffectLevel.EXTERNAL_MUTATION,
      ),
      *[
        CapabilitySpec(
          capability_id=capability_id,
          name=name,
          kind="tool",
          input_schema={"type": "object"},
          output_schema={"type": "object"},
          side_effect_level=SideEffectLevel.READ,
        )
        for capability_id, name in [
          (AtomicCapabilityIds.SKILL_OPEN, "Open skill details"),
          (AtomicCapabilityIds.MEMORY_SEARCH, "Search memory"),
          (AtomicCapabilityIds.MEMORY_READ, "Read memory"),
          (AtomicCapabilityIds.ARTIFACT_READ, "Read artifact metadata"),
          (AtomicCapabilityIds.EVENT_SEARCH, "Search runtime events"),
          (AtomicCapabilityIds.CONTEXT_EXPAND, "Expand context refs"),
        ]
      ],
      CapabilitySpec(
        capability_id=AtomicCapabilityIds.CONTEXT_COMPACT,
        name="Compact context into memory",
        kind="tool",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        side_effect_level=SideEffectLevel.WRITE,
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
      self._schema(
        "browser_scan",
        (
          "Inspect browser targets and a bounded page summary through a control workbench. "
          "Use tabs_only=true to only list tabs. After navigate returns target_id, keep passing the same target_id. "
          "For dynamic feed/card pages, avoid repeated full scans; use browser_execute_js to refresh, scroll, click, "
          "or extract structured visible cards."
        ),
        [],
        {"tabs_only": {"type": "boolean"}, "target_id": {"type": "string"}},
      ),
      self._schema(
        "browser_execute_js",
        (
          "Execute JavaScript in a browser target for precise browser control and DOM extraction. "
          "Do not use location.href/open for first navigation of an exploratory task; use browser_navigate without target_id "
          "so the workbench can create and own a new tab. "
          "Prefer this over repeated browser_scan when reading dynamic pages. Return compact JSON for extracted data; "
          "for feed/latest-post tasks extract visible cards with title/text/url/author/time/metrics after refresh or scroll."
        ),
        ["code"],
        {"target_id": {"type": "string"}},
      ),
      self._schema(
        "browser_navigate",
        (
          "Navigate a browser target to a URL. Omit target_id for exploratory/search/open-page tasks to create a new "
          "owned tab; pass target_id only when continuing in a tab returned by browser_navigate/browser_scan or when "
          "the user explicitly asked to operate the current tab."
        ),
        ["url"],
        {"target_id": {"type": "string"}},
      ),
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
      self._schema(
        "agent_delegate",
        "Start an asynchronous child-agent task through a configured connector.",
        ["parent_run_id", "connector_id", "task"],
        {
          "parent_run_id": {"type": "string"},
          "parent_agent_id": {"type": "string"},
          "parent_session_id": {"type": "string"},
          "connector_id": {"type": "string"},
          "agent_type": {"type": "string"},
          "task": {"type": "string"},
          "metadata": {"type": "object"},
        },
      ),
      self._schema(
        "agent_delegation_status",
        "Inspect one or more delegation tasks scoped to the parent run.",
        ["parent_run_id"],
        {
          "parent_run_id": {"type": "string"},
          "task_ids": {"type": "array", "items": {"type": "string"}},
          "wait_ms": {"type": "integer"},
        },
      ),
      self._schema(
        "agent_cancel_delegation",
        "Cancel a running delegation task scoped to the parent run.",
        ["parent_run_id", "task_id"],
        {
          "parent_run_id": {"type": "string"},
          "task_id": {"type": "string"},
          "reason": {"type": "string"},
        },
      ),
      self._schema(
        "skill_open",
        "Open a selected skill's full spec when compact SkillCard/index context is not enough.",
        ["skill_id"],
        {
          "skill_id": {"type": "string"},
          "detail_level": {"type": "string", "enum": ["card", "spec"]},
        },
      ),
      self._schema(
        "memory_search",
        "Search working, episodic, semantic, procedural, or artifact memory in the current scope.",
        ["query"],
        {
          "query": {"type": "string"},
          "memory_type": {"type": "string", "enum": ["working", "episodic", "semantic", "procedural", "artifact"]},
          "limit": {"type": "integer"},
        },
      ),
      self._schema(
        "memory_read",
        "Read one memory item by memory_id.",
        ["memory_id"],
        {"memory_id": {"type": "string"}},
      ),
      self._schema(
        "artifact_read",
        "Read artifact reference, metadata, and a bounded content view when an adapter can resolve it.",
        ["artifact_id"],
        {
          "artifact_id": {"type": "string"},
          "include_content": {"type": "boolean"},
          "start": {"type": "integer"},
          "count": {"type": "integer"},
          "keyword": {"type": "string"},
        },
      ),
      self._schema(
        "event_search",
        "Search runtime events by run_id, event_type, or query text.",
        [],
        {
          "run_id": {"type": "string"},
          "event_type": {"type": "string"},
          "query": {"type": "string"},
          "limit": {"type": "integer"},
        },
      ),
      self._schema(
        "context_compact",
        "Store a compact summary of older context or tool results as episodic memory.",
        ["summary"],
        {
          "summary": {"type": "string"},
          "outcome": {"type": "string"},
          "observations": {"type": "array", "items": {"type": "string"}},
          "event_ids": {"type": "array", "items": {"type": "string"}},
          "artifact_ids": {"type": "array", "items": {"type": "string"}},
        },
      ),
      self._schema(
        "context_expand",
        "Expand memory, artifact, event, or skill refs omitted from the compact prompt.",
        [],
        {
          "memory_ids": {"type": "array", "items": {"type": "string"}},
          "artifact_ids": {"type": "array", "items": {"type": "string"}},
          "event_ids": {"type": "array", "items": {"type": "string"}},
          "skill_ids": {"type": "array", "items": {"type": "string"}},
        },
      ),
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

  async def delegate_agent(self, input: dict[str, Any]) -> ToolResult:
    if self._delegation is None:
      return ToolResult.failure("adapter_not_configured", "Agent delegation broker is not configured.")
    parent_run_id = self._required_string(input, "parent_run_id")
    connector_id = self._required_string(input, "connector_id")
    task = self._required_string(input, "task")
    for value in (parent_run_id, connector_id, task):
      if isinstance(value, ToolResult):
        return value
    metadata = input.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
      return ToolResult.failure("invalid_input", "metadata must be an object when provided.")
    try:
      report = await self._delegation.delegate(
        parent_run_id=parent_run_id,
        parent_agent_id=self._optional_string(input.get("parent_agent_id")),
        parent_session_id=self._optional_string(input.get("parent_session_id")),
        connector_id=connector_id,
        agent_type=self._optional_string(input.get("agent_type")),
        task=task,
        metadata=metadata,
      )
    except (KeyError, ValueError) as exc:
      return ToolResult.failure("delegation_error", str(exc))
    return ToolResult.success({"delegation": report.to_dict()})

  async def get_delegation_status(self, input: dict[str, Any]) -> ToolResult:
    if self._delegation is None:
      return ToolResult.failure("adapter_not_configured", "Agent delegation broker is not configured.")
    parent_run_id = self._required_string(input, "parent_run_id")
    if isinstance(parent_run_id, ToolResult):
      return parent_run_id
    task_ids = input.get("task_ids")
    if task_ids is not None and not (
      isinstance(task_ids, list) and all(isinstance(task_id, str) and task_id for task_id in task_ids)
    ):
      return ToolResult.failure("invalid_input", "task_ids must be a list of non-empty strings.")
    wait_ms = input.get("wait_ms")
    if wait_ms is not None:
      try:
        wait_ms = int(wait_ms)
      except (TypeError, ValueError):
        return ToolResult.failure("invalid_input", "wait_ms must be an integer.")
    try:
      reports = await self._delegation.get_status(
        parent_run_id=parent_run_id,
        task_ids=task_ids,
        wait_ms=wait_ms,
      )
    except ValueError as exc:
      return ToolResult.failure("delegation_error", str(exc))
    return ToolResult.success({"delegations": [report.to_dict() for report in reports]})

  async def cancel_delegation(self, input: dict[str, Any]) -> ToolResult:
    if self._delegation is None:
      return ToolResult.failure("adapter_not_configured", "Agent delegation broker is not configured.")
    parent_run_id = self._required_string(input, "parent_run_id")
    task_id = self._required_string(input, "task_id")
    for value in (parent_run_id, task_id):
      if isinstance(value, ToolResult):
        return value
    try:
      report = await self._delegation.cancel(
        parent_run_id=parent_run_id,
        task_id=task_id,
        reason=str(input.get("reason") or "delegation cancelled"),
      )
    except ValueError as exc:
      return ToolResult.failure("delegation_error", str(exc))
    return ToolResult.success({"delegation": report.to_dict()})

  def open_skill(self, input: dict[str, Any]) -> ToolResult:
    if self._skill_service is None:
      return ToolResult.failure("adapter_not_configured", "Skill service is not configured.")
    skill_id = self._required_string(input, "skill_id")
    if isinstance(skill_id, ToolResult):
      return skill_id
    skill = self._skill_service.get(skill_id)
    if skill is None:
      return ToolResult.failure("not_found", f"Skill not found: {skill_id}")
    data = skill.to_dict()
    if str(input.get("detail_level") or "spec") == "card":
      data = {
        key: data.get(key)
        for key in [
          "skill_id",
          "name",
          "description",
          "when_to_use",
          "status",
          "execution_mode",
          "recommended_tools",
          "recommended_workflows",
          "use_policy",
        ]
      }
    return ToolResult.success({"skill": data})

  def search_memory(self, input: dict[str, Any]) -> ToolResult:
    if self._memory is None:
      return ToolResult.failure("adapter_not_configured", "Memory facade is not configured.")
    scope = str(input.get("scope") or "default")
    query = str(input.get("query") or "").strip()
    memory_type = self._optional_string(input.get("memory_type"))
    limit = _positive_int(input.get("limit"), default=5, maximum=20)
    memories = self._memory.retrieve(scope, memory_type=memory_type, limit=100)
    ranked = sorted(
      (
        (_memory_score(memory, query), memory)
        for memory in memories
      ),
      key=lambda item: item[0],
      reverse=True,
    )
    if query:
      ranked = [item for item in ranked if item[0] > 0]
    return ToolResult.success(
      {
        "scope": scope,
        "query": query,
        "memory_type": memory_type,
        "results": [
          _memory_summary(memory, score=score)
          for score, memory in ranked[:limit]
        ],
      }
    )

  def read_memory(self, input: dict[str, Any]) -> ToolResult:
    if self._uow_factory is None:
      return ToolResult.failure("adapter_not_configured", "Unit of work factory is not configured.")
    memory_id = self._required_string(input, "memory_id")
    if isinstance(memory_id, ToolResult):
      return memory_id
    with self._uow_factory() as uow:
      memory = uow.memory.get(memory_id)
      if memory is not None:
        uow.memory.mark_used(memory_id)
    if memory is None:
      return ToolResult.failure("not_found", f"Memory not found: {memory_id}")
    return ToolResult.success({"memory": _memory_detail(memory)})

  def read_artifact(self, input: dict[str, Any]) -> ToolResult:
    if self._uow_factory is None:
      return ToolResult.failure("adapter_not_configured", "Unit of work factory is not configured.")
    artifact_id = self._required_string(input, "artifact_id")
    if isinstance(artifact_id, ToolResult):
      return artifact_id
    with self._uow_factory() as uow:
      artifact = uow.artifacts.get(artifact_id)
      metadata = uow.artifacts.get_metadata(artifact_id)
    if artifact is None:
      return ToolResult.failure("not_found", f"Artifact not found: {artifact_id}")
    content_result = self._read_artifact_content(
      artifact,
      metadata or {},
      include_content=input.get("include_content", True),
      start=input.get("start"),
      count=input.get("count"),
      keyword=input.get("keyword"),
    )
    return ToolResult.success(
      {
        "artifact": artifact.to_dict(),
        "metadata": metadata or {},
        **content_result,
      },
      artifact_refs=[artifact],
    )

  def search_events(self, input: dict[str, Any]) -> ToolResult:
    if self._uow_factory is None:
      return ToolResult.failure("adapter_not_configured", "Unit of work factory is not configured.")
    run_id = self._optional_string(input.get("run_id"))
    event_type = self._optional_string(input.get("event_type"))
    query = str(input.get("query") or "").strip().lower()
    limit = _positive_int(input.get("limit"), default=10, maximum=50)
    with self._uow_factory() as uow:
      events = uow.events.list_by_run(run_id) if run_id else uow.events.list_all()
    if event_type:
      events = [event for event in events if event.event_type.value == event_type]
    if query:
      events = [
        event
        for event in events
        if query in str(event.payload).lower()
        or query in event.event_type.value.lower()
        or query in event.event_id.lower()
      ]
    return ToolResult.success(
      {
        "run_id": run_id,
        "event_type": event_type,
        "query": query,
        "events": [_event_summary(event) for event in events[-limit:]],
      }
    )

  def compact_context(self, input: dict[str, Any]) -> ToolResult:
    if self._memory is None:
      return ToolResult.failure("adapter_not_configured", "Memory facade is not configured.")
    scope = str(input.get("scope") or input.get("run_id") or "default")
    summary = self._required_string(input, "summary")
    if isinstance(summary, ToolResult):
      return summary
    observations = input.get("observations") or [summary]
    if not isinstance(observations, list):
      return ToolResult.failure("invalid_input", "observations must be a list when provided.")
    event_ids = input.get("event_ids") or []
    if not isinstance(event_ids, list):
      return ToolResult.failure("invalid_input", "event_ids must be a list when provided.")
    artifact_refs = self._artifact_refs_from_ids(input.get("artifact_ids"))
    item = self._memory.write_episode(
      scope=scope,
      task_id=self._optional_string(input.get("task_id")),
      event_ids=[str(event_id) for event_id in event_ids],
      observations=[str(observation) for observation in observations],
      outcome=str(input.get("outcome") or summary),
      artifact_refs=artifact_refs,
      importance=0.75,
      created_by="context_compact",
    )
    return ToolResult.success(
      {
        "memory_id": item.memory_id,
        "scope": scope,
        "memory_type": item.memory_type,
        "summary": item.content.get("summary"),
      },
      artifact_refs=artifact_refs,
    )

  def expand_context(self, input: dict[str, Any]) -> ToolResult:
    expanded: dict[str, Any] = {}
    memory_ids = _string_list(input.get("memory_ids"))
    artifact_ids = _string_list(input.get("artifact_ids"))
    event_ids = _string_list(input.get("event_ids"))
    skill_ids = _string_list(input.get("skill_ids"))
    if memory_ids:
      expanded["memories"] = [
        result.output["memory"]
        for memory_id in memory_ids
        if (result := self.read_memory({"memory_id": memory_id})).ok
      ]
    if artifact_ids:
      expanded["artifacts"] = [
        result.output
        for artifact_id in artifact_ids
        if (result := self.read_artifact({"artifact_id": artifact_id})).ok
      ]
    if event_ids:
      if self._uow_factory is None:
        return ToolResult.failure("adapter_not_configured", "Unit of work factory is not configured.")
      with self._uow_factory() as uow:
        events = [event for event_id in event_ids if (event := uow.events.get(event_id)) is not None]
      expanded["events"] = [_event_detail(event) for event in events]
    if skill_ids:
      expanded["skills"] = [
        result.output["skill"]
        for skill_id in skill_ids
        if (result := self.open_skill({"skill_id": skill_id, "detail_level": "spec"})).ok
      ]
    return ToolResult.success({"expanded": expanded})

  def _artifact_refs_from_ids(self, raw_ids: object) -> list[ArtifactRef]:
    artifact_ids = _string_list(raw_ids)
    if self._uow_factory is None or not artifact_ids:
      return []
    with self._uow_factory() as uow:
      return [ref for artifact_id in artifact_ids if (ref := uow.artifacts.get(artifact_id)) is not None]

  def _read_artifact_content(
    self,
    artifact: ArtifactRef,
    metadata: dict[str, Any],
    *,
    include_content: object,
    start: object = None,
    count: object = None,
    keyword: object = None,
  ) -> dict[str, Any]:
    if include_content is False:
      return {"content_available": False, "content_status": "not_requested"}
    inline = _inline_artifact_content(metadata)
    if inline is not None:
      return {
        "content_available": True,
        "content_source": "metadata",
        "content": self._select_text_view(inline, start=start, count=count, keyword=keyword),
      }
    if self._file_workspace is not None:
      path = _artifact_file_path(artifact.uri)
      if path:
        try:
          content = self._file_workspace.read_text(path)
        except (OSError, PermissionError) as exc:
          return {
            "content_available": False,
            "content_status": "read_failed",
            "content_error": str(exc),
          }
        return {
          "content_available": True,
          "content_source": "file_workspace",
          "content": self._select_text_view(content, start=start, count=count, keyword=keyword),
        }
    return {
      "content_available": False,
      "content_status": "adapter_not_available",
      "note": "No artifact content adapter matched this URI/media type.",
    }

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
    body_summary = _summarize_http_body(response.body)
    anti_bot = _detect_anti_bot(response.url, response.body, body_summary)
    output = {
      "status": response.status,
      "headers": response.headers,
      "body": response.body,
      "body_summary": body_summary,
      "url": response.url,
    }
    if anti_bot is not None:
      output["access_issue"] = anti_bot
    return ToolResult.success(
      output
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
  def _optional_string(value: object) -> str | None:
    if value is None:
      return None
    return str(value)

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
