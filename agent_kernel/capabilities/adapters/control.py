"""Generic control workbench adapters.

The kernel models browser, desktop, and mobile control as workbench commands.
Concrete integrations such as browser-link HTTP bridges, Win32 desktop drivers, ADB, or platform UIA live
behind this protocol so policy/runtime code does not depend on one tool stack.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from dataclasses import dataclass, field
import io
import json
import re
import shutil
import subprocess
import time
from typing import Any, Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from agent_kernel.domain.base import DomainModel, new_id


ControlTargetKind = Literal["browser", "desktop", "mobile"]
ControlActionKind = Literal[
  "inspect",
  "execute_js",
  "navigate",
  "screenshot",
  "click",
  "key",
  "type_text",
  "dump_ui",
  "tap",
]
BrowserLeaseMode = Literal["read", "mutation", "exclusive"]


_BROWSER_PAGE_SUMMARY_JS = r"""
(() => {
  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const unique = (values) => {
    const seen = new Set();
    const out = [];
    for (const value of values) {
      const text = clean(value);
      if (!text || seen.has(text)) continue;
      seen.add(text);
      out.push(text);
    }
    return out;
  };
  const html = document.documentElement ? document.documentElement.outerHTML : '';
  const feedTitles = unique(
    Array.from(html.matchAll(/"displayTitle"\s*:\s*"([^"]+)"/g)).map((m) => {
      try { return JSON.parse('"' + m[1] + '"'); } catch (_) { return m[1]; }
    })
  ).slice(0, 30);
  const visibleCards = unique(
    Array.from(document.querySelectorAll('article, [class*="note"], [class*="feed"], a, h1, h2, h3'))
      .map((el) => clean(el.innerText || el.textContent || ''))
      .filter((text) => text.length >= 2 && text.length <= 160)
  ).slice(0, 30);
  const normalizeUrl = (href) => {
    try { return new URL(href, location.href).href; } catch (_) { return ''; }
  };
  const isBlockedHref = (href) => (
    !href ||
    href.startsWith('javascript:') ||
    href.startsWith('mailto:') ||
    href.startsWith('tel:') ||
    href.includes('127.0.0.1') ||
    href.includes('localhost')
  );
  const isNoiseTitle = (text) => /^(百度首页|每天充电学习|抗击肺炎|hao123|更多产品|图片|视频|资讯|地图|贴吧|文库|知道|学术|更多|设置|登录|首页|上一页|下一页)$/.test(text);
  const mainRoots = Array.from(new Set([
    ...Array.from(document.querySelectorAll('#content_left, #b_results, #search, #rso, #center_col, main')),
    document.body
  ].filter(Boolean)));
  const links = mainRoots.flatMap((root) => Array.from(root.querySelectorAll('a[href]'))).map((el) => {
    const text = clean(el.innerText || el.textContent || el.getAttribute('aria-label') || '');
    const href = normalizeUrl(el.getAttribute('href') || '');
    return { text, href };
  }).filter((item) => item.text && !isBlockedHref(item.href));
  const resultLinks = unique(
    links
      .filter((item) => item.text.length >= 4 && item.text.length <= 180)
      .filter((item) => !isNoiseTitle(item.text))
      .map((item) => JSON.stringify(item))
  ).map((item) => JSON.parse(item)).slice(0, 20);
  const searchResultSelectors = [
    '#content_left .result',
    '#content_left .c-container',
    '#b_results .b_algo',
    '#search .g',
    '#rso .g',
    '#center_col .g',
    '[data-sokoban-container]',
    '.result',
    '.c-container',
    '[class*="result"]',
    '[tpl]',
    'article',
    '.g',
    '.b_algo'
  ];
  const searchResultNodes = Array.from(new Set(
    searchResultSelectors.flatMap((selector) => Array.from(document.querySelectorAll(selector)))
  ));
  const titleFromNode = (node) => {
    const anchors = Array.from(node.querySelectorAll('h1 a[href], h2 a[href], h3 a[href], [role="heading"] a[href], a[href]'));
    return anchors.map((anchor) => {
      const title = clean(anchor.innerText || anchor.textContent || anchor.getAttribute('aria-label') || '');
      const href = normalizeUrl(anchor.getAttribute('href') || '');
      return { title, href };
    }).find((item) => item.title.length >= 4 && item.title.length <= 220 && !isBlockedHref(item.href) && !isNoiseTitle(item.title));
  };
  const snippetFromNode = (node, title) => {
    const parts = unique(
      Array.from(node.querySelectorAll('[class*="abstract"], [class*="summary"], [class*="content"], .c-abstract, .c-span-last, p, span, div'))
        .map((el) => clean(el.innerText || el.textContent || ''))
        .filter((text) => text && text !== title && text.length >= 8 && text.length <= 600)
    );
    const explicit = parts.find((text) => !text.includes('百度快照') && !isNoiseTitle(text));
    const full = clean(node.innerText || node.textContent || '');
    return clean(explicit || full.replace(title, '')).slice(0, 500);
  };
  const searchResults = unique(
    searchResultNodes.map((node) => {
      const candidate = titleFromNode(node);
      if (!candidate) return null;
      const title = candidate.title;
      const href = candidate.href;
      const snippet = snippetFromNode(node, title);
      if (!snippet && clean(node.innerText || node.textContent || '').length < title.length + 12) return null;
      return JSON.stringify({ title, href, snippet });
    }).filter(Boolean)
  ).map((item) => JSON.parse(item)).slice(0, 12);
  const text = clean(document.body ? document.body.innerText : '').slice(0, 12000);
  return {
    url: location.href,
    title: document.title,
    feed_titles: feedTitles,
    visible_cards: visibleCards,
    links: resultLinks,
    search_results: searchResults,
    text,
  };
})()
"""


@dataclass(slots=True)
class ControlTarget(DomainModel):
  target_id: str
  kind: ControlTargetKind
  label: str | None = None
  metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ControlCommand(DomainModel):
  command_id: str
  target_kind: ControlTargetKind
  action: ControlActionKind
  target_id: str | None = None
  payload: dict[str, Any] = field(default_factory=dict)
  timeout_seconds: float | None = None

  @classmethod
  def create(
    cls,
    target_kind: ControlTargetKind,
    action: ControlActionKind,
    target_id: str | None = None,
    payload: dict[str, Any] | None = None,
    timeout_seconds: float | None = None,
  ) -> "ControlCommand":
    return cls(
      command_id=new_id("control_cmd"),
      target_kind=target_kind,
      action=action,
      target_id=target_id,
      payload=payload or {},
      timeout_seconds=timeout_seconds,
    )


@dataclass(slots=True)
class ControlResult(DomainModel):
  ok: bool
  output: dict[str, Any] = field(default_factory=dict)
  error: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ControlOwnerContext:
  """Logical owner of a control action.

  The backend still deals in physical targets. This context lets the
  workbench bind browser tabs to the run/agent scope that created or claimed
  them without coupling browser governance to a concrete adapter.
  """

  run_id: str
  agent_id: str | None = None
  task_id: str | None = None
  scope: str | None = None
  workbench_id: str | None = None

  @property
  def owner_key(self) -> tuple[str, str | None, str | None, str | None]:
    return (self.scope or self.run_id, self.workbench_id, self.agent_id, self.task_id)


@dataclass(slots=True)
class BrowserTargetOwnership(DomainModel):
  target_id: str
  owner_run_id: str
  owner_agent_id: str | None = None
  owner_task_id: str | None = None
  scope: str | None = None
  workbench_id: str | None = None
  purpose: str | None = None
  last_action: str | None = None

  def owner_key(self) -> tuple[str, str | None, str | None, str | None]:
    return (self.scope or self.owner_run_id, self.workbench_id, self.owner_agent_id, self.owner_task_id)


@dataclass(slots=True)
class BrowserTargetLease(DomainModel):
  lease_id: str
  target_id: str
  mode: BrowserLeaseMode
  owner_run_id: str
  owner_agent_id: str | None = None
  owner_task_id: str | None = None
  scope: str | None = None
  workbench_id: str | None = None

  def owner_key(self) -> tuple[str, str | None, str | None, str | None]:
    return (self.scope or self.owner_run_id, self.workbench_id, self.owner_agent_id, self.owner_task_id)


@dataclass(slots=True)
class BrowserTargetResolution:
  target_id: str | None
  lease: BrowserTargetLease | None = None
  error: dict[str, Any] | None = None


class BrowserTargetCoordinator:
  """In-process browser target ownership and lease coordinator.

  This is deliberately a small interface-facing service. It does not know how
  BrowserLink, Playwright, or another bridge controls tabs; it only decides
  which physical target a logical run/agent scope may read or mutate.
  """

  def __init__(self) -> None:
    self._ownership_by_target: dict[str, BrowserTargetOwnership] = {}
    self._active_target_by_owner: dict[tuple[str, str | None, str | None, str | None], str] = {}
    self._active_lease_by_target: dict[str, BrowserTargetLease] = {}

  def resolve(
    self,
    *,
    action: ControlActionKind,
    target_id: str | None,
    targets: list[ControlTarget],
    owner: ControlOwnerContext | None,
    payload: dict[str, Any] | None = None,
  ) -> BrowserTargetResolution:
    if owner is None:
      return BrowserTargetResolution(target_id=target_id)
    if action == "inspect" and bool((payload or {}).get("tabs_only", False)):
      return BrowserTargetResolution(target_id=target_id)
    resolved_target_id = target_id or self._active_target_by_owner.get(owner.owner_key)
    if resolved_target_id is None:
      if action == "navigate":
        return BrowserTargetResolution(target_id=None)
      if action == "inspect":
        return BrowserTargetResolution(
          target_id=None,
          error={
            "type": "browser_target_scope_required",
            "message": (
              "Reading page content requires an explicit target_id or an active browser target owned by this run scope. "
              "Use browser_scan(tabs_only=true) to list tabs, then pass a target_id or open a new owned tab with browser_navigate."
            ),
          },
        )
      else:
        return BrowserTargetResolution(
          target_id=None,
          error={
            "type": "browser_target_scope_required",
            "message": "Browser mutation/read action requires an explicit target_id or a target already owned by this run scope.",
          },
        )
    if resolved_target_id is None:
      return BrowserTargetResolution(target_id=None)
    ownership = self._ownership_by_target.get(resolved_target_id)
    if ownership is not None and ownership.owner_key() != owner.owner_key:
      return BrowserTargetResolution(
        target_id=resolved_target_id,
        error={
          "type": "browser_target_owned_by_other_scope",
          "target_id": resolved_target_id,
          "owner": self._ownership_dict(ownership),
        },
      )
    lease_mode = self._lease_mode_for_action(action)
    lease = self._acquire_lease(resolved_target_id, lease_mode, owner)
    if lease is None:
      active = self._active_lease_by_target.get(resolved_target_id)
      return BrowserTargetResolution(
        target_id=resolved_target_id,
        error={
          "type": "browser_target_lease_conflict",
          "target_id": resolved_target_id,
          "active_lease": active.to_dict() if active is not None else None,
        },
      )
    return BrowserTargetResolution(target_id=resolved_target_id, lease=lease)

  def bind_result(
    self,
    *,
    action: ControlActionKind,
    requested_target_id: str | None,
    result: ControlResult,
    owner: ControlOwnerContext | None,
    lease: BrowserTargetLease | None,
    purpose: str | None = None,
  ) -> ControlResult:
    try:
      if owner is not None and result.ok:
        target_id = self._result_target_id(result.output) or requested_target_id
        if action == "navigate" and requested_target_id is None and not target_id:
          result = ControlResult(
            ok=False,
            output=dict(result.output),
            error={
              "type": "browser_target_creation_unconfirmed",
              "message": (
                "Browser navigation requested a new owned tab, but the browser bridge did not return or expose "
                "a target matching the requested URL. The run will not claim an arbitrary existing tab."
              ),
            },
          )
        ownership = self._ownership_by_target.get(target_id) if target_id else None
        if (
          action == "inspect"
          and requested_target_id is None
          and target_id
          and ownership is not None
          and ownership.owner_key() != owner.owner_key
          and not self._can_globally_observe(owner)
        ):
          result.output.pop("page", None)
          result.output["page_error"] = {
            "type": "browser_target_owned_by_other_scope",
            "target_id": target_id,
            "owner": self._ownership_dict(ownership),
            "message": "This target is visible in the tab list but page content is scoped to another owner.",
          }
        if target_id and (ownership is None or ownership.owner_key() == owner.owner_key):
          self.claim(target_id, owner, purpose=purpose, last_action=action)
      if owner is not None:
        self._annotate_result_output(result.output, owner, lease)
      return result
    finally:
      if lease is not None:
        self.release(lease)

  def claim(
    self,
    target_id: str,
    owner: ControlOwnerContext,
    *,
    purpose: str | None = None,
    last_action: str | None = None,
  ) -> BrowserTargetOwnership:
    ownership = BrowserTargetOwnership(
      target_id=target_id,
      owner_run_id=owner.run_id,
      owner_agent_id=owner.agent_id,
      owner_task_id=owner.task_id,
      scope=owner.scope,
      workbench_id=owner.workbench_id,
      purpose=purpose,
      last_action=last_action,
    )
    self._ownership_by_target[target_id] = ownership
    self._active_target_by_owner[owner.owner_key] = target_id
    return ownership

  def release(self, lease: BrowserTargetLease) -> None:
    active = self._active_lease_by_target.get(lease.target_id)
    if active is not None and active.lease_id == lease.lease_id:
      self._active_lease_by_target.pop(lease.target_id, None)

  def ownerships(self) -> list[BrowserTargetOwnership]:
    return list(self._ownership_by_target.values())

  def annotate_targets(self, targets: list[dict[str, Any]], owner: ControlOwnerContext | None = None) -> None:
    for target in targets:
      target_id = target.get("target_id")
      if not isinstance(target_id, str):
        continue
      ownership = self._ownership_by_target.get(target_id)
      if ownership is not None:
        target["ownership"] = self._ownership_dict(ownership)
        if owner is not None:
          target["owned_by_current_scope"] = ownership.owner_key() == owner.owner_key
      active_lease = self._active_lease_by_target.get(target_id)
      if active_lease is not None:
        target["active_lease"] = active_lease.to_dict()

  def _select_claimable_target(self, targets: list[ControlTarget], owner: ControlOwnerContext) -> str | None:
    candidates = [
      target
      for target in targets
      if not _is_browser_extension_or_local_target(target)
      and (
        target.target_id not in self._ownership_by_target
        or self._ownership_by_target[target.target_id].owner_key() == owner.owner_key
      )
    ]
    return candidates[-1].target_id if candidates else None

  def _acquire_lease(
    self,
    target_id: str,
    mode: BrowserLeaseMode,
    owner: ControlOwnerContext,
  ) -> BrowserTargetLease | None:
    active = self._active_lease_by_target.get(target_id)
    if active is not None and active.owner_key() != owner.owner_key:
      return None
    lease = BrowserTargetLease(
      lease_id=new_id("browser_lease"),
      target_id=target_id,
      mode=mode,
      owner_run_id=owner.run_id,
      owner_agent_id=owner.agent_id,
      owner_task_id=owner.task_id,
      scope=owner.scope,
      workbench_id=owner.workbench_id,
    )
    self._active_lease_by_target[target_id] = lease
    return lease

  @staticmethod
  def _lease_mode_for_action(action: ControlActionKind) -> BrowserLeaseMode:
    if action == "inspect":
      return "read"
    if action in {"navigate", "execute_js"}:
      return "mutation"
    return "exclusive"

  @staticmethod
  def _result_target_id(output: dict[str, Any]) -> str | None:
    for key in ("target_id", "active_target_id"):
      value = output.get(key)
      if isinstance(value, str) and value:
        return value
    target = output.get("target")
    if isinstance(target, dict):
      value = target.get("target_id")
      if isinstance(value, str) and value:
        return value
    return None

  def _annotate_result_output(
    self,
    output: dict[str, Any],
    owner: ControlOwnerContext,
    lease: BrowserTargetLease | None,
  ) -> None:
    targets = output.get("targets")
    if isinstance(targets, list):
      self.annotate_targets([target for target in targets if isinstance(target, dict)], owner)
    active_target_id = self._active_target_by_owner.get(owner.owner_key)
    output["browser_scope"] = {
      "scope": owner.scope or owner.run_id,
      "run_id": owner.run_id,
      "agent_id": owner.agent_id,
      "task_id": owner.task_id,
      "active_target_id": active_target_id,
      "lease": lease.to_dict() if lease is not None else None,
    }

  @staticmethod
  def _can_globally_observe(owner: ControlOwnerContext) -> bool:
    return owner.agent_id is None and owner.task_id is None

  @staticmethod
  def _ownership_dict(ownership: BrowserTargetOwnership) -> dict[str, Any]:
    return {
      "target_id": ownership.target_id,
      "run_id": ownership.owner_run_id,
      "agent_id": ownership.owner_agent_id,
      "task_id": ownership.owner_task_id,
      "scope": ownership.scope,
      "workbench_id": ownership.workbench_id,
      "purpose": ownership.purpose,
      "last_action": ownership.last_action,
    }


class ControlBackend(Protocol):
  def list_targets(self, kind: ControlTargetKind | None = None) -> list[ControlTarget]:
    ...

  async def execute(self, command: ControlCommand) -> ControlResult:
    ...


class FakeControlBackend:
  """Deterministic control backend for tests and local dry-runs."""

  def __init__(self) -> None:
    self.targets: dict[str, ControlTarget] = {}
    self.responses: dict[tuple[ControlTargetKind, ControlActionKind], ControlResult] = {}
    self.commands: list[ControlCommand] = []

  def register_target(self, target: ControlTarget) -> None:
    self.targets[target.target_id] = target

  def register_response(
    self,
    target_kind: ControlTargetKind,
    action: ControlActionKind,
    result: ControlResult,
  ) -> None:
    self.responses[(target_kind, action)] = result

  def list_targets(self, kind: ControlTargetKind | None = None) -> list[ControlTarget]:
    targets = list(self.targets.values())
    if kind is None:
      return targets
    return [target for target in targets if target.kind == kind]

  async def execute(self, command: ControlCommand) -> ControlResult:
    self.commands.append(command)
    if command.target_id is not None and command.target_id not in self.targets:
      return ControlResult(
        ok=False,
        error={"type": "control_target_not_found", "target_id": command.target_id},
      )
    return self.responses.get(
      (command.target_kind, command.action),
      ControlResult(
        ok=False,
        error={
          "type": "control_action_not_found",
          "target_kind": command.target_kind,
          "action": command.action,
        },
      ),
    )


class BrowserLinkHTTPBackend:
  """Browser control backend for `/link`-style HTTP browser bridges."""

  def __init__(
    self,
    base_url: str = "http://127.0.0.1:18766/link",
    request_timeout_seconds: float = 30.0,
    post_json: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
  ) -> None:
    self._base_url = base_url
    self._request_timeout_seconds = request_timeout_seconds
    self._post_json = post_json or self._post_sync_http

  def list_targets(self, kind: ControlTargetKind | None = None) -> list[ControlTarget]:
    if kind not in (None, "browser"):
      return []
    try:
      response = self._post_json({"cmd": "get_all_sessions"})
    except RuntimeError:
      return []
    sessions = response.get("r", [])
    if not isinstance(sessions, list):
      return []
    return [self._target_from_session(session) for session in sessions if isinstance(session, dict)]

  async def execute(self, command: ControlCommand) -> ControlResult:
    try:
      if command.target_kind != "browser":
        return ControlResult(
          ok=False,
          error={"type": "unsupported_control_target", "target_kind": command.target_kind},
        )
      if command.action == "inspect":
        return await asyncio.to_thread(self._inspect, command)
      if command.action == "execute_js":
        return await asyncio.to_thread(self._execute_js, command)
      if command.action == "navigate":
        return await asyncio.to_thread(self._navigate, command)
      return ControlResult(
        ok=False,
        error={"type": "unsupported_control_action", "action": command.action},
      )
    except RuntimeError as exc:
      return ControlResult(ok=False, error={"type": "browser_link_unavailable", "message": str(exc)})

  def _inspect(self, command: ControlCommand) -> ControlResult:
    targets = [target.to_dict() for target in self.list_targets("browser")]
    tabs_only = bool(command.payload.get("tabs_only", False))
    if command.target_id is None and tabs_only:
      return ControlResult(ok=True, output={"targets": targets})
    session_id = self._resolve_scan_session_id(command.target_id)
    output: dict[str, Any] = {"targets": targets}
    if session_id is None:
      return ControlResult(ok=True, output=output)
    page = self._inspect_page(session_id, command.timeout_seconds)
    if page.ok:
      output["active_target_id"] = session_id
      output["page"] = page.output.get("page", page.output)
    else:
      output["page_error"] = page.error
    return ControlResult(ok=True, output=output)

  def _resolve_scan_session_id(self, target_id: str | None) -> str | None:
    if target_id is not None:
      response = self._post_json({"cmd": "find_session", "url_pattern": target_id})
      matched = response.get("r", [])
      if isinstance(matched, list) and matched:
        item = matched[0]
        if self._is_match(item):
          return str(item[0])
      return target_id
    targets = self.list_targets("browser")
    active_like = [
      target
      for target in targets
      if not _is_browser_extension_or_local_target(target)
    ]
    return active_like[-1].target_id if active_like else (targets[-1].target_id if targets else None)

  def _inspect_page(self, session_id: str, timeout_seconds: float | None = None) -> ControlResult:
    result = self._execute_js(
      ControlCommand(
        command_id="inspect_page",
        target_kind="browser",
        action="execute_js",
        target_id=session_id,
        payload={"code": _BROWSER_PAGE_SUMMARY_JS},
        timeout_seconds=timeout_seconds,
      )
    )
    if not result.ok:
      return result
    raw = result.output.get("result")
    page = raw.get("data") if isinstance(raw, dict) else raw
    return ControlResult(ok=True, output={"page": page if isinstance(page, dict) else {"value": page}})

  def _execute_js(self, command: ControlCommand) -> ControlResult:
    code = command.payload.get("code")
    if not isinstance(code, str):
      return ControlResult(ok=False, error={"type": "invalid_control_command", "message": "code is required"})
    response = self._post_json(
      {
        "cmd": "execute_js",
        "sessionId": command.target_id,
        "code": code,
        "timeout": str(command.timeout_seconds or self._request_timeout_seconds),
      }
    )
    result = response.get("r", {})
    if isinstance(result, dict) and result.get("error"):
      return ControlResult(ok=False, error={"type": "browser_link_error", "message": str(result["error"])})
    return ControlResult(ok=True, output={"result": result})

  def _navigate(self, command: ControlCommand) -> ControlResult:
    url = command.payload.get("url")
    if not isinstance(url, str):
      return ControlResult(ok=False, error={"type": "invalid_control_command", "message": "url is required"})
    if command.target_id is None:
      created = self._create_tab(url, command.timeout_seconds)
      if created.ok:
        output = dict(created.output)
        output["url"] = url
        return ControlResult(ok=True, output=output)
      return created
    return self._navigate_existing_target(command.target_id, url, command.timeout_seconds, command_id=command.command_id)

  def _navigate_existing_target(
    self,
    target_id: str,
    url: str,
    timeout_seconds: float | None = None,
    *,
    command_id: str = "navigate_existing",
  ) -> ControlResult:
    code = "window.location.href = " + json.dumps(url) + ";"
    result = self._execute_js(
      ControlCommand(
        command_id=command_id,
        target_kind="browser",
        action="execute_js",
        target_id=target_id,
        payload={"code": code},
        timeout_seconds=timeout_seconds,
      )
    )
    if not result.ok:
      return result
    output = dict(result.output)
    output["url"] = url
    output["target_id"] = target_id
    output["active_target_id"] = target_id
    return ControlResult(ok=True, output=output)

  def _create_tab(self, url: str, timeout_seconds: float | None = None) -> ControlResult:
    command = {"cmd": "tabs", "method": "create", "url": url, "active": True}
    result = self._execute_js(
      ControlCommand(
        command_id="create_tab",
        target_kind="browser",
        action="execute_js",
        target_id=None,
        payload={"code": json.dumps(command, ensure_ascii=False)},
        timeout_seconds=timeout_seconds,
      )
    )
    if not result.ok:
      return result
    raw = result.output.get("result")
    data = raw.get("data") if isinstance(raw, dict) else raw
    target = self._target_from_created_tab_payload(data, url)
    if target is None:
      if _has_created_tab_payload(data):
        target = self._target_from_existing_sessions(url)
        if target is None:
          reusable_target = self._target_from_same_origin_sessions(url)
          if reusable_target is not None:
            result = self._navigate_existing_target(
              reusable_target.target_id,
              url,
              timeout_seconds,
              command_id="reuse_same_origin_tab",
            )
            if result.ok:
              result.output["reused_existing_target"] = True
              result.output["reuse_reason"] = "same_origin_create_fallback"
            return result
      else:
        target = self._target_from_existing_sessions_with_retry(url, timeout_seconds)
    if target is not None:
      return ControlResult(
        ok=True,
        output={
          "target_id": target.target_id,
          "active_target_id": target.target_id,
          "target": target.to_dict(),
        },
      )
    return ControlResult(
      ok=False,
      output={"result": raw, "url": url},
      error={
        "type": "browser_target_creation_unconfirmed",
        "message": (
          "The browser bridge accepted a tab creation request but did not return a created tab, and no existing "
          "browser session matched the requested URL."
        ),
      },
    )

  def _target_from_created_tab_payload(self, data: object, requested_url: str) -> ControlTarget | None:
    if isinstance(data, dict):
      if not _browser_urls_equivalent(data.get("url"), requested_url):
        return None
      return self._target_from_tab_like(data, requested_url)
    if not isinstance(data, list):
      return None
    tabs = [item for item in data if isinstance(item, dict) and item.get("id") is not None]
    if not tabs:
      return None
    matching = [item for item in tabs if _browser_urls_equivalent(item.get("url"), requested_url)]
    active_matching = [item for item in matching if item.get("active") is True]
    selected = (active_matching or matching or [None])[-1]
    if selected is None:
      return None
    return self._target_from_tab_like(selected, requested_url)

  def _target_from_existing_sessions(self, requested_url: str) -> ControlTarget | None:
    matching = [
      target
      for target in self.list_targets("browser")
      if _browser_urls_equivalent(target.metadata.get("url"), requested_url)
    ]
    return matching[-1] if matching else None

  def _target_from_same_origin_sessions(self, requested_url: str) -> ControlTarget | None:
    matching = [
      target
      for target in self.list_targets("browser")
      if _browser_origins_equivalent(target.metadata.get("url"), requested_url)
    ]
    return matching[-1] if matching else None

  def _target_from_existing_sessions_with_retry(
    self,
    requested_url: str,
    timeout_seconds: float | None = None,
  ) -> ControlTarget | None:
    wait_seconds = min(max(timeout_seconds or self._request_timeout_seconds, 0.2), 1.5)
    deadline = time.monotonic() + wait_seconds
    while True:
      target = self._target_from_existing_sessions(requested_url)
      if target is not None:
        return target
      if time.monotonic() >= deadline:
        return None
      time.sleep(0.1)

  def _target_from_tab_like(self, tab: dict[str, Any], requested_url: str) -> ControlTarget | None:
    tab_id = tab.get("id")
    if tab_id is None:
      return None
    metadata = {
      key: value
      for key, value in tab.items()
      if key not in {"id", "title"} and isinstance(key, str)
    }
    metadata.setdefault("url", requested_url)
    metadata["created"] = True
    return ControlTarget(
      target_id=str(tab_id),
      kind="browser",
      label=tab.get("title") if isinstance(tab.get("title"), str) else None,
      metadata=metadata,
    )

  def _post_sync_http(self, payload: dict[str, Any]) -> dict[str, Any]:
    try:
      request = Request(
        self._base_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
      )
      with urlopen(request, timeout=self._request_timeout_seconds) as response:
        body = response.read().decode("utf-8")
    except HTTPError as exc:
      raise RuntimeError(f"Browser link HTTP error {exc.code}: {exc.reason}") from exc
    except URLError as exc:
      raise RuntimeError(f"Browser link connection failed: {exc.reason}") from exc
    except TimeoutError as exc:
      raise RuntimeError("Browser link request timed out.") from exc
    try:
      value = json.loads(body)
    except json.JSONDecodeError as exc:
      raise RuntimeError("Browser link returned invalid JSON.") from exc
    if not isinstance(value, dict):
      raise RuntimeError("Browser link response must be a JSON object.")
    return value

  @staticmethod
  def _target_from_session(session: dict[str, Any]) -> ControlTarget:
    target_id = str(session.get("id", ""))
    return ControlTarget(
      target_id=target_id,
      kind="browser",
      label=session.get("title") if isinstance(session.get("title"), str) else None,
      metadata={
        key: value
        for key, value in session.items()
        if key not in {"id", "title"} and isinstance(key, str)
      },
    )

  @classmethod
  def _target_from_match(cls, item: Any) -> ControlTarget:
    session_id, info = item
    session = dict(info)
    session["id"] = session_id
    return cls._target_from_session(session)

  @staticmethod
  def _is_match(item: Any) -> bool:
    return (
      isinstance(item, (list, tuple))
      and len(item) == 2
      and isinstance(item[0], str)
      and isinstance(item[1], dict)
    )


TMWebDriverHTTPBackend = BrowserLinkHTTPBackend


def _is_browser_extension_or_local_target(target: ControlTarget) -> bool:
  url = str(target.metadata.get("url") or "")
  if not url:
    return False
  return (
    url.startswith("chrome://")
    or url.startswith("chrome-extension://")
    or url.startswith("devtools://")
    or url.startswith("http://127.0.0.1:")
    or url.startswith("http://localhost:")
  )


def _browser_urls_equivalent(candidate: object, requested: str) -> bool:
  if not isinstance(candidate, str):
    return False
  left = _normalize_browser_url(candidate)
  right = _normalize_browser_url(requested)
  return bool(left and right and left == right)


def _browser_origins_equivalent(candidate: object, requested: str) -> bool:
  if not isinstance(candidate, str):
    return False
  try:
    left = urlsplit(candidate)
    right = urlsplit(requested)
  except ValueError:
    return False
  return bool(
    left.scheme
    and right.scheme
    and left.hostname
    and right.hostname
    and left.scheme == right.scheme
    and left.hostname == right.hostname
    and (left.port or _default_port(left.scheme)) == (right.port or _default_port(right.scheme))
  )


def _default_port(scheme: str) -> int | None:
  if scheme == "http":
    return 80
  if scheme == "https":
    return 443
  return None


def _has_created_tab_payload(data: object) -> bool:
  if isinstance(data, dict):
    return data.get("id") is not None
  if isinstance(data, list):
    return any(isinstance(item, dict) and item.get("id") is not None for item in data)
  return False


def _normalize_browser_url(value: str) -> str:
  text = value.strip()
  if not text:
    return ""
  text = text.split("#", 1)[0]
  if text.endswith("/"):
    text = text[:-1]
  return text


@dataclass(slots=True)
class CommandResult:
  returncode: int
  stdout: str = ""
  stderr: str = ""
  stdout_bytes: bytes = b""


CommandRunner = Callable[[list[str], float | None], CommandResult]


class DesktopUIDetector(Protocol):
  def dump(self, target: int | str) -> list[dict[str, Any]]:
    ...


class VisionDetector(Protocol):
  def detect(
    self,
    image_bytes: bytes,
    target_kind: ControlTargetKind,
    target_id: str | None = None,
  ) -> list[dict[str, Any]]:
    ...


@dataclass(slots=True)
class HTTPVisionEndpoint:
  url: str
  timeout_seconds: float = 30.0
  headers: dict[str, str] = field(default_factory=dict)


class DriverVisionDetector:
  """Adapts a vision driver into normalized control nodes."""

  def __init__(self, driver: Any) -> None:
    self._driver = driver

  def detect(
    self,
    image_bytes: bytes,
    target_kind: ControlTargetKind,
    target_id: str | None = None,
  ) -> list[dict[str, Any]]:
    if hasattr(self._driver, "detect"):
      detections = self._driver.detect(image_bytes, target_kind=target_kind, target_id=target_id)
    elif hasattr(self._driver, "detect_image"):
      detections = self._driver.detect_image(image_bytes)
    else:
      raise RuntimeError("Vision driver must expose detect(image_bytes, ...) or detect_image(image_bytes).")
    return [self._normalize_detection(detection) for detection in detections]

  @classmethod
  def _normalize_detection(cls, detection: Any) -> dict[str, Any]:
    if isinstance(detection, dict):
      source = detection
    else:
      source = {
        "label": getattr(detection, "label", ""),
        "text": getattr(detection, "text", ""),
        "confidence": getattr(detection, "confidence", None),
        "bounds": getattr(detection, "bounds", None),
        "clickable": getattr(detection, "clickable", True),
      }
    bounds = UIAStyleDesktopDetector._normalize_bounds(source.get("bounds"))
    cx, cy = UIAStyleDesktopDetector._bounds_center(bounds)
    return {
      "text": str(source.get("text") or source.get("label") or ""),
      "label": str(source.get("label") or source.get("text") or ""),
      "source": "vision",
      "confidence": source.get("confidence"),
      "click": bool(source.get("clickable", True)),
      "cx": cx,
      "cy": cy,
      "bounds": bounds,
    }


class HTTPVisionDetector:
  """Adapts an HTTP computer-vision service into normalized control nodes."""

  def __init__(
    self,
    endpoint: HTTPVisionEndpoint | str,
    post_json: Callable[[dict[str, Any]], dict[str, Any] | list[Any]] | None = None,
  ) -> None:
    self._endpoint = endpoint if isinstance(endpoint, HTTPVisionEndpoint) else HTTPVisionEndpoint(endpoint)
    self._post_json = post_json or self._post_sync_http

  def detect(
    self,
    image_bytes: bytes,
    target_kind: ControlTargetKind,
    target_id: str | None = None,
  ) -> list[dict[str, Any]]:
    response = self._post_json(
      {
        "image": {
          "media_type": "image/png",
          "base64": base64.b64encode(image_bytes).decode("ascii"),
        },
        "target_kind": target_kind,
        "target_id": target_id,
      }
    )
    detections = self._extract_detections(response)
    return [DriverVisionDetector._normalize_detection(detection) for detection in detections]

  def _post_sync_http(self, payload: dict[str, Any]) -> dict[str, Any] | list[Any]:
    try:
      request = Request(
        self._endpoint.url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
          "Content-Type": "application/json",
          "Accept": "application/json",
          **self._endpoint.headers,
        },
        method="POST",
      )
      with urlopen(request, timeout=self._endpoint.timeout_seconds) as response:
        body = response.read().decode("utf-8")
    except HTTPError as exc:
      raise RuntimeError(f"Vision service HTTP error {exc.code}: {exc.reason}") from exc
    except URLError as exc:
      raise RuntimeError(f"Vision service connection failed: {exc.reason}") from exc
    except TimeoutError as exc:
      raise RuntimeError("Vision service request timed out.") from exc
    try:
      value = json.loads(body)
    except json.JSONDecodeError as exc:
      raise RuntimeError("Vision service returned invalid JSON.") from exc
    if not isinstance(value, (dict, list)):
      raise RuntimeError("Vision service response must be a JSON object or array.")
    return value

  @staticmethod
  def _extract_detections(response: dict[str, Any] | list[Any]) -> list[Any]:
    if isinstance(response, list):
      return response
    for key in ("detections", "nodes", "results", "items"):
      value = response.get(key)
      if isinstance(value, list):
        return value
    raise RuntimeError("Vision service response must contain a detections, nodes, results, or items array.")


class UIAStyleDesktopDetector:
  """Normalizes UIA-like desktop element trees into control nodes."""

  def __init__(self, driver: Any) -> None:
    self._driver = driver

  def dump(self, target: int | str) -> list[dict[str, Any]]:
    if hasattr(self._driver, "dump_tree"):
      elements = self._driver.dump_tree(target)
    elif hasattr(self._driver, "iter_elements"):
      elements = self._driver.iter_elements(target)
    else:
      raise RuntimeError("UIA-style driver must expose dump_tree(target) or iter_elements(target).")
    return [self._normalize_element(element) for element in elements]

  @classmethod
  def _normalize_element(cls, element: Any) -> dict[str, Any]:
    if isinstance(element, dict):
      source = element
    else:
      source = {
        "name": getattr(element, "name", ""),
        "control_type": getattr(element, "control_type", ""),
        "automation_id": getattr(element, "automation_id", ""),
        "class_name": getattr(element, "class_name", ""),
        "bounds": getattr(element, "bounds", None),
        "clickable": getattr(element, "clickable", False),
        "enabled": getattr(element, "enabled", None),
      }
    bounds = cls._normalize_bounds(source.get("bounds"))
    cx, cy = cls._bounds_center(bounds)
    return {
      "text": str(source.get("text") or source.get("name") or ""),
      "control_type": str(source.get("control_type") or source.get("type") or ""),
      "automation_id": str(source.get("automation_id") or source.get("id") or ""),
      "class_name": str(source.get("class_name") or source.get("cls") or ""),
      "click": bool(source.get("clickable") or source.get("click")),
      "enabled": source.get("enabled"),
      "cx": cx,
      "cy": cy,
      "bounds": bounds,
    }

  @staticmethod
  def _normalize_bounds(value: Any) -> list[int]:
    if isinstance(value, dict):
      left = int(value.get("left", 0))
      top = int(value.get("top", 0))
      right = int(value.get("right", value.get("x2", left)))
      bottom = int(value.get("bottom", value.get("y2", top)))
      return [left, top, right, bottom]
    if isinstance(value, (list, tuple)) and len(value) == 4:
      return [int(value[0]), int(value[1]), int(value[2]), int(value[3])]
    return [0, 0, 0, 0]

  @staticmethod
  def _bounds_center(bounds: list[int]) -> tuple[int, int]:
    if len(bounds) != 4:
      return 0, 0
    return (bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2


class UIAutomationDesktopDetector:
  """Desktop UI detector backed by the optional `uiautomation` package."""

  def __init__(self, uiautomation: Any | None = None, max_depth: int = 8) -> None:
    self._uiautomation = uiautomation
    self._max_depth = max_depth

  def dump(self, target: int | str) -> list[dict[str, Any]]:
    uiautomation = self._load_uiautomation()
    root = self._root_control(uiautomation, target)
    nodes: list[dict[str, Any]] = []
    self._collect(root, nodes, depth=0)
    return nodes

  def _load_uiautomation(self) -> Any:
    if self._uiautomation is not None:
      return self._uiautomation
    import importlib

    try:
      self._uiautomation = importlib.import_module("uiautomation")
    except ImportError as exc:
      raise RuntimeError("uiautomation package is not available.") from exc
    return self._uiautomation

  @staticmethod
  def _root_control(uiautomation: Any, target: int | str) -> Any:
    if target not in ("", None) and hasattr(uiautomation, "ControlFromHandle"):
      return uiautomation.ControlFromHandle(int(target) if str(target).isdigit() else target)
    if hasattr(uiautomation, "GetRootControl"):
      return uiautomation.GetRootControl()
    raise RuntimeError("uiautomation module must expose ControlFromHandle or GetRootControl.")

  def _collect(self, control: Any, nodes: list[dict[str, Any]], depth: int) -> None:
    if control is None or depth > self._max_depth:
      return
    nodes.append(UIAStyleDesktopDetector._normalize_element(self._control_to_mapping(control)))
    for child in self._children(control):
      self._collect(child, nodes, depth + 1)

  @staticmethod
  def _children(control: Any) -> list[Any]:
    if hasattr(control, "GetChildren"):
      children = control.GetChildren()
    elif hasattr(control, "children"):
      children = control.children
    else:
      children = []
    return list(children or [])

  @staticmethod
  def _control_to_mapping(control: Any) -> dict[str, Any]:
    rect = getattr(control, "BoundingRectangle", None)
    return {
      "name": getattr(control, "Name", getattr(control, "name", "")),
      "control_type": getattr(control, "ControlTypeName", getattr(control, "control_type", "")),
      "automation_id": getattr(control, "AutomationId", getattr(control, "automation_id", "")),
      "class_name": getattr(control, "ClassName", getattr(control, "class_name", "")),
      "bounds": UIAutomationDesktopDetector._rect_to_bounds(rect),
      "clickable": bool(getattr(control, "IsEnabled", getattr(control, "enabled", True))),
      "enabled": getattr(control, "IsEnabled", getattr(control, "enabled", None)),
    }

  @staticmethod
  def _rect_to_bounds(rect: Any) -> list[int]:
    if rect is None:
      return [0, 0, 0, 0]
    return [
      int(getattr(rect, "left", getattr(rect, "Left", 0))),
      int(getattr(rect, "top", getattr(rect, "Top", 0))),
      int(getattr(rect, "right", getattr(rect, "Right", 0))),
      int(getattr(rect, "bottom", getattr(rect, "Bottom", 0))),
    ]


class ADBMobileBackend:
  """Mobile control backend using adb-compatible commands."""

  def __init__(
    self,
    adb_path: str | None = None,
    command_timeout_seconds: float = 15.0,
    runner: CommandRunner | None = None,
    vision_detector: VisionDetector | None = None,
  ) -> None:
    self._adb_path = adb_path or shutil.which("adb") or "adb"
    self._command_timeout_seconds = command_timeout_seconds
    self._runner = runner or self._run_subprocess
    self._vision_detector = vision_detector

  def list_targets(self, kind: ControlTargetKind | None = None) -> list[ControlTarget]:
    if kind not in (None, "mobile"):
      return []
    result = self._run([self._adb_path, "devices"], timeout_seconds=self._command_timeout_seconds)
    if not result.ok:
      return []
    return self._parse_devices(result.output.get("stdout", ""))

  async def execute(self, command: ControlCommand) -> ControlResult:
    if command.target_kind != "mobile":
      return ControlResult(
        ok=False,
        error={"type": "unsupported_control_target", "target_kind": command.target_kind},
      )
    if command.action == "dump_ui":
      return await asyncio.to_thread(self._dump_ui, command)
    if command.action == "tap":
      return await asyncio.to_thread(self._tap, command)
    if command.action == "type_text":
      return await asyncio.to_thread(self._type_text, command)
    if command.action == "key":
      return await asyncio.to_thread(self._key, command)
    if command.action == "screenshot":
      return await asyncio.to_thread(self._screenshot, command)
    return ControlResult(ok=False, error={"type": "unsupported_control_action", "action": command.action})

  def _dump_ui(self, command: ControlCommand) -> ControlResult:
    target_args = self._target_args(command.target_id)
    dump = self._run(
      [self._adb_path, *target_args, "shell", "uiautomator", "dump", "--compressed", "/sdcard/window.xml"],
      timeout_seconds=command.timeout_seconds or self._command_timeout_seconds,
    )
    if not dump.ok:
      return dump
    cat = self._run(
      [self._adb_path, *target_args, "shell", "cat", "/sdcard/window.xml"],
      timeout_seconds=command.timeout_seconds or self._command_timeout_seconds,
    )
    if not cat.ok:
      return cat
    try:
      nodes = self._parse_ui_xml(cat.output.get("stdout", ""))
    except ElementTree.ParseError as exc:
      return ControlResult(ok=False, error={"type": "invalid_android_ui_xml", "message": str(exc)})
    nodes.extend(self._vision_nodes(command))
    return ControlResult(ok=True, output={"nodes": nodes, "raw_xml": cat.output.get("stdout", "")})

  def _tap(self, command: ControlCommand) -> ControlResult:
    try:
      x, y = self._payload_xy(command.payload)
    except ValueError as exc:
      return ControlResult(ok=False, error={"type": "invalid_control_command", "message": str(exc)})
    return self._run(
      [self._adb_path, *self._target_args(command.target_id), "shell", "input", "tap", str(x), str(y)],
      timeout_seconds=command.timeout_seconds or self._command_timeout_seconds,
    )

  def _type_text(self, command: ControlCommand) -> ControlResult:
    text = command.payload.get("text")
    if not isinstance(text, str):
      return ControlResult(ok=False, error={"type": "invalid_control_command", "message": "text is required"})
    return self._run(
      [
        self._adb_path,
        *self._target_args(command.target_id),
        "shell",
        "input",
        "text",
        self._escape_adb_text(text),
      ],
      timeout_seconds=command.timeout_seconds or self._command_timeout_seconds,
    )

  def _key(self, command: ControlCommand) -> ControlResult:
    key = command.payload.get("key")
    if not isinstance(key, str):
      return ControlResult(ok=False, error={"type": "invalid_control_command", "message": "key is required"})
    return self._run(
      [self._adb_path, *self._target_args(command.target_id), "shell", "input", "keyevent", key],
      timeout_seconds=command.timeout_seconds or self._command_timeout_seconds,
    )

  def _screenshot(self, command: ControlCommand) -> ControlResult:
    result = self._runner(
      [self._adb_path, *self._target_args(command.target_id), "exec-out", "screencap", "-p"],
      command.timeout_seconds or self._command_timeout_seconds,
    )
    if result.returncode != 0:
      return ControlResult(
        ok=False,
        error={
          "type": "adb_command_failed",
          "returncode": result.returncode,
          "stderr": result.stderr,
        },
      )
    return ControlResult(
      ok=True,
      output={
        "media_type": "image/png",
        "base64": base64.b64encode(result.stdout_bytes).decode("ascii"),
      },
    )

  def _vision_nodes(self, command: ControlCommand) -> list[dict[str, Any]]:
    if self._vision_detector is None:
      return []
    screenshot = self._screenshot(command)
    if not screenshot.ok:
      return []
    image_base64 = screenshot.output.get("base64")
    if not isinstance(image_base64, str):
      return []
    try:
      image_bytes = base64.b64decode(image_base64)
      return self._vision_detector.detect(image_bytes, "mobile", command.target_id)
    except Exception:
      return []

  def _run(self, argv: list[str], timeout_seconds: float | None) -> ControlResult:
    try:
      result = self._runner(argv, timeout_seconds)
    except FileNotFoundError as exc:
      return ControlResult(ok=False, error={"type": "adb_not_found", "message": str(exc)})
    except TimeoutError as exc:
      return ControlResult(ok=False, error={"type": "adb_timeout", "message": str(exc)})
    if result.returncode != 0:
      return ControlResult(
        ok=False,
        error={
          "type": "adb_command_failed",
          "returncode": result.returncode,
          "stderr": result.stderr,
        },
      )
    return ControlResult(
      ok=True,
      output={
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode,
      },
    )

  @staticmethod
  def _run_subprocess(argv: list[str], timeout_seconds: float | None) -> CommandResult:
    completed = subprocess.run(
      argv,
      capture_output=True,
      timeout=timeout_seconds,
      check=False,
    )
    return CommandResult(
      returncode=completed.returncode,
      stdout=completed.stdout.decode("utf-8", errors="replace"),
      stderr=completed.stderr.decode("utf-8", errors="replace"),
      stdout_bytes=completed.stdout,
    )

  @staticmethod
  def _parse_devices(stdout: str) -> list[ControlTarget]:
    targets: list[ControlTarget] = []
    for line in stdout.splitlines()[1:]:
      parts = line.split()
      if len(parts) < 2 or parts[1] != "device":
        continue
      targets.append(
        ControlTarget(
          target_id=parts[0],
          kind="mobile",
          label=parts[0],
          metadata={"transport": "adb"},
        )
      )
    return targets

  @staticmethod
  def _parse_ui_xml(xml: str) -> list[dict[str, Any]]:
    root = ElementTree.fromstring(xml)
    nodes: list[dict[str, Any]] = []
    for node in root.iter("node"):
      package = node.get("package", "")
      if "termux" in package.lower():
        continue
      text = node.get("text", "")
      desc = node.get("content-desc", "")
      bounds = node.get("bounds", "")
      class_name = node.get("class", "").split(".")[-1]
      resource_id = node.get("resource-id", "")
      clickable = node.get("clickable") == "true"
      cx, cy = ADBMobileBackend._bounds_center(bounds)
      nodes.append(
        {
          "text": text or desc,
          "click": clickable,
          "edit": class_name == "EditText",
          "cx": cx,
          "cy": cy,
          "cls": class_name,
          "rid": resource_id,
          "bounds": bounds,
        }
      )
    return nodes

  @staticmethod
  def _bounds_center(bounds: str) -> tuple[int, int]:
    matches = re.findall(r"\[(\d+),(\d+)\]", bounds)
    if len(matches) != 2:
      return 0, 0
    return (
      (int(matches[0][0]) + int(matches[1][0])) // 2,
      (int(matches[0][1]) + int(matches[1][1])) // 2,
    )

  @staticmethod
  def _payload_xy(payload: dict[str, Any]) -> tuple[int, int]:
    x = payload.get("x")
    y = payload.get("y")
    if not isinstance(x, int) or not isinstance(y, int):
      raise ValueError("payload.x and payload.y are required integer coordinates.")
    return x, y

  @staticmethod
  def _target_args(target_id: str | None) -> list[str]:
    return ["-s", target_id] if target_id else []

  @staticmethod
  def _escape_adb_text(text: str) -> str:
    return text.replace(" ", "%s")


class Win32DesktopBackend:
  """Desktop control backend using an optional Win32 desktop driver."""

  def __init__(
    self,
    desktop_driver: Any | None = None,
    win32gui: Any | None = None,
    clipboard: Any | None = None,
    ui_detector: DesktopUIDetector | None = None,
    vision_detector: VisionDetector | None = None,
  ) -> None:
    self._desktop_driver = desktop_driver
    self._win32gui = win32gui
    self._clipboard = clipboard
    self._ui_detector = ui_detector
    self._vision_detector = vision_detector

  def list_targets(self, kind: ControlTargetKind | None = None) -> list[ControlTarget]:
    if kind not in (None, "desktop"):
      return []
    win32gui = self._load_win32gui()
    if win32gui is None:
      return []
    targets: list[ControlTarget] = []

    def collect(hwnd: int, _extra: object) -> None:
      if hasattr(win32gui, "IsWindowVisible") and not win32gui.IsWindowVisible(hwnd):
        return
      title = win32gui.GetWindowText(hwnd) if hasattr(win32gui, "GetWindowText") else ""
      if not title:
        return
      metadata: dict[str, Any] = {}
      if hasattr(win32gui, "GetWindowRect"):
        metadata["rect"] = list(win32gui.GetWindowRect(hwnd))
      if hasattr(win32gui, "GetClassName"):
        metadata["class_name"] = win32gui.GetClassName(hwnd)
      targets.append(
        ControlTarget(
          target_id=str(hwnd),
          kind="desktop",
          label=title,
          metadata=metadata,
        )
      )

    win32gui.EnumWindows(collect, None)
    return targets

  async def execute(self, command: ControlCommand) -> ControlResult:
    if command.target_kind != "desktop":
      return ControlResult(
        ok=False,
        error={"type": "unsupported_control_target", "target_kind": command.target_kind},
      )
    if command.action == "inspect":
      return ControlResult(ok=True, output={"targets": [target.to_dict() for target in self.list_targets("desktop")]})
    if command.action == "screenshot":
      return await asyncio.to_thread(self._screenshot, command)
    if command.action == "click":
      return await asyncio.to_thread(self._click, command)
    if command.action == "key":
      return await asyncio.to_thread(self._key, command)
    if command.action == "type_text":
      return await asyncio.to_thread(self._type_text, command)
    if command.action == "dump_ui":
      return await asyncio.to_thread(self._dump_ui, command)
    return ControlResult(ok=False, error={"type": "unsupported_control_action", "action": command.action})

  def _screenshot(self, command: ControlCommand) -> ControlResult:
    desktop_driver = self._load_desktop_driver()
    if desktop_driver is None:
      return ControlResult(ok=False, error={"type": "desktop_driver_not_available"})
    target = self._desktop_target(command.target_id)
    try:
      image = desktop_driver.GrabWindow(target)
      png_bytes = self._image_to_png_bytes(image)
    except Exception as exc:
      return ControlResult(ok=False, error={"type": "desktop_screenshot_failed", "message": str(exc)})
    return ControlResult(
      ok=True,
      output={
        "media_type": "image/png",
        "base64": base64.b64encode(png_bytes).decode("ascii"),
      },
    )

  def _click(self, command: ControlCommand) -> ControlResult:
    desktop_driver = self._load_desktop_driver()
    if desktop_driver is None:
      return ControlResult(ok=False, error={"type": "desktop_driver_not_available"})
    try:
      x, y = self._payload_xy(command.payload)
      self._activate(command.target_id)
      returned = desktop_driver.Click(x, y)
    except ValueError as exc:
      return ControlResult(ok=False, error={"type": "invalid_control_command", "message": str(exc)})
    except Exception as exc:
      return ControlResult(ok=False, error={"type": "desktop_click_failed", "message": str(exc)})
    return ControlResult(ok=True, output={"x": x, "y": y, "result": self._safe_output(returned)})

  def _key(self, command: ControlCommand) -> ControlResult:
    desktop_driver = self._load_desktop_driver()
    if desktop_driver is None:
      return ControlResult(ok=False, error={"type": "desktop_driver_not_available"})
    key = command.payload.get("key")
    if not isinstance(key, str):
      return ControlResult(ok=False, error={"type": "invalid_control_command", "message": "key is required"})
    try:
      self._activate(command.target_id)
      returned = desktop_driver.Press(key)
    except Exception as exc:
      return ControlResult(ok=False, error={"type": "desktop_key_failed", "message": str(exc)})
    return ControlResult(ok=True, output={"key": key, "result": self._safe_output(returned)})

  def _type_text(self, command: ControlCommand) -> ControlResult:
    text = command.payload.get("text")
    if not isinstance(text, str):
      return ControlResult(ok=False, error={"type": "invalid_control_command", "message": "text is required"})
    clipboard = self._load_clipboard()
    desktop_driver = self._load_desktop_driver()
    if clipboard is None or desktop_driver is None:
      return ControlResult(ok=False, error={"type": "desktop_text_input_not_available"})
    try:
      self._activate(command.target_id)
      clipboard.copy(text)
      returned = desktop_driver.Press("ctrl+v")
    except Exception as exc:
      return ControlResult(ok=False, error={"type": "desktop_type_text_failed", "message": str(exc)})
    return ControlResult(ok=True, output={"chars": len(text), "result": self._safe_output(returned)})

  def _dump_ui(self, command: ControlCommand) -> ControlResult:
    if self._ui_detector is None and self._vision_detector is None:
      return ControlResult(ok=False, error={"type": "desktop_ui_detector_not_configured"})
    nodes: list[dict[str, Any]] = []
    if self._ui_detector is not None:
      try:
        nodes.extend(self._ui_detector.dump(self._desktop_target(command.target_id)))
      except Exception as exc:
        return ControlResult(ok=False, error={"type": "desktop_ui_dump_failed", "message": str(exc)})
    nodes.extend(self._vision_nodes(command))
    return ControlResult(ok=True, output={"nodes": nodes})

  def _vision_nodes(self, command: ControlCommand) -> list[dict[str, Any]]:
    if self._vision_detector is None:
      return []
    screenshot = self._screenshot(command)
    if not screenshot.ok:
      return []
    image_base64 = screenshot.output.get("base64")
    if not isinstance(image_base64, str):
      return []
    try:
      image_bytes = base64.b64decode(image_base64)
      return self._vision_detector.detect(image_bytes, "desktop", command.target_id)
    except Exception:
      return []

  def _activate(self, target_id: str | None) -> None:
    desktop_driver = self._load_desktop_driver()
    if target_id is None or desktop_driver is None or not hasattr(desktop_driver, "Activate"):
      return
    desktop_driver.Activate(self._desktop_target(target_id))

  def _load_desktop_driver(self) -> Any | None:
    if self._desktop_driver is not None:
      return self._desktop_driver
    return None

  def _load_win32gui(self) -> Any | None:
    if self._win32gui is not None:
      return self._win32gui
    import importlib

    try:
      self._win32gui = importlib.import_module("win32gui")
    except ImportError:
      return None
    return self._win32gui

  def _load_clipboard(self) -> Any | None:
    if self._clipboard is not None:
      return self._clipboard
    try:
      self._clipboard = importlib.import_module("pyperclip")
    except ImportError:
      return None
    return self._clipboard

  @staticmethod
  def _desktop_target(target_id: str | None) -> int | str:
    if target_id is None:
      return ""
    return int(target_id) if target_id.isdigit() else target_id

  @staticmethod
  def _payload_xy(payload: dict[str, Any]) -> tuple[int, int]:
    x = payload.get("x")
    y = payload.get("y")
    if not isinstance(x, int) or not isinstance(y, int):
      raise ValueError("payload.x and payload.y are required integer coordinates.")
    return x, y

  @staticmethod
  def _image_to_png_bytes(image: Any) -> bytes:
    if isinstance(image, bytes):
      return image
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()

  @staticmethod
  def _safe_output(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool, dict, list)):
      return value
    return str(value)


class ControlWorkbench:
  """High-level control API used by capability runtime workbench calls."""

  def __init__(self, backend: ControlBackend, browser_coordinator: BrowserTargetCoordinator | None = None) -> None:
    self._backend = backend
    self._browser_coordinator = browser_coordinator or BrowserTargetCoordinator()

  def list_targets(self, kind: ControlTargetKind | None = None) -> list[ControlTarget]:
    return self._backend.list_targets(kind)

  @property
  def browser_coordinator(self) -> BrowserTargetCoordinator:
    return self._browser_coordinator

  async def execute_command(
    self,
    command: ControlCommand,
    owner: ControlOwnerContext | None = None,
  ) -> ControlResult:
    if command.target_kind == "browser":
      return await self._execute_browser_command(command, owner)
    return await self._backend.execute(command)

  async def inspect_browser(
    self,
    target_id: str | None = None,
    payload: dict[str, Any] | None = None,
    owner: ControlOwnerContext | None = None,
  ) -> ControlResult:
    return await self._execute_browser_command(ControlCommand.create("browser", "inspect", target_id, payload or {}), owner)

  async def execute_js(
    self,
    code: str,
    target_id: str | None = None,
    timeout_seconds: float | None = None,
    owner: ControlOwnerContext | None = None,
  ) -> ControlResult:
    command = ControlCommand.create(
      "browser",
      "execute_js",
      target_id,
      {"code": code},
      timeout_seconds=timeout_seconds,
    )
    return await self._execute_browser_command(
      command,
      owner,
    )

  async def navigate(
    self,
    url: str,
    target_id: str | None = None,
    timeout_seconds: float | None = None,
    owner: ControlOwnerContext | None = None,
  ) -> ControlResult:
    command = ControlCommand.create(
      "browser",
      "navigate",
      target_id,
      {"url": url},
      timeout_seconds=timeout_seconds,
    )
    return await self._execute_browser_command(
      command,
      owner,
    )

  async def screenshot(self, target_kind: ControlTargetKind, target_id: str | None = None) -> ControlResult:
    return await self._backend.execute(ControlCommand.create(target_kind, "screenshot", target_id))

  async def click(
    self,
    target_kind: ControlTargetKind,
    x: int,
    y: int,
    target_id: str | None = None,
  ) -> ControlResult:
    return await self._backend.execute(
      ControlCommand.create(target_kind, "click", target_id, {"x": x, "y": y})
    )

  async def key(
    self,
    target_kind: ControlTargetKind,
    key: str,
    target_id: str | None = None,
  ) -> ControlResult:
    return await self._backend.execute(
      ControlCommand.create(target_kind, "key", target_id, {"key": key})
    )

  async def type_text(
    self,
    target_kind: ControlTargetKind,
    text: str,
    target_id: str | None = None,
  ) -> ControlResult:
    return await self._backend.execute(
      ControlCommand.create(target_kind, "type_text", target_id, {"text": text})
    )

  async def dump_ui(self, target_kind: ControlTargetKind, target_id: str | None = None) -> ControlResult:
    return await self._backend.execute(ControlCommand.create(target_kind, "dump_ui", target_id))

  async def tap(self, x: int, y: int, target_id: str | None = None) -> ControlResult:
    return await self._backend.execute(
      ControlCommand.create("mobile", "tap", target_id, {"x": x, "y": y})
    )

  async def _execute_browser_command(
    self,
    command: ControlCommand,
    owner: ControlOwnerContext | None,
  ) -> ControlResult:
    if owner is None:
      return await self._backend.execute(command)
    targets = self._backend.list_targets("browser")
    resolution = self._browser_coordinator.resolve(
      action=command.action,
      target_id=command.target_id,
      targets=targets,
      owner=owner,
      payload=command.payload,
    )
    if resolution.error is not None:
      if command.action == "inspect" and resolution.error.get("type") == "browser_target_scope_required":
        targets = [target.to_dict() for target in targets]
        result = ControlResult(
          ok=True,
          output={"targets": targets, "page_error": resolution.error},
        )
        self._browser_coordinator.annotate_targets(targets, owner)
        self._browser_coordinator._annotate_result_output(result.output, owner, resolution.lease)
        return result
      return ControlResult(ok=False, error=resolution.error)
    scoped_command = ControlCommand(
      command_id=command.command_id,
      target_kind=command.target_kind,
      action=command.action,
      target_id=resolution.target_id,
      payload=command.payload,
      timeout_seconds=command.timeout_seconds,
    )
    result = await self._backend.execute(scoped_command)
    purpose = scoped_command.payload.get("url") if isinstance(scoped_command.payload.get("url"), str) else None
    return self._browser_coordinator.bind_result(
      action=scoped_command.action,
      requested_target_id=scoped_command.target_id,
      result=result,
      owner=owner,
      lease=resolution.lease,
      purpose=purpose,
    )
