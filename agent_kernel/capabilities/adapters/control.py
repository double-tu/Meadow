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
from typing import Any, Literal, Protocol
from urllib.error import HTTPError, URLError
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
    if command.target_id is None:
      targets = [target.to_dict() for target in self.list_targets("browser")]
      return ControlResult(ok=True, output={"targets": targets})
    response = self._post_json({"cmd": "find_session", "url_pattern": command.target_id})
    matched = response.get("r", [])
    if not isinstance(matched, list):
      return ControlResult(ok=False, error={"type": "invalid_browser_link_response", "response": response})
    targets = [self._target_from_match(item).to_dict() for item in matched if self._is_match(item)]
    return ControlResult(ok=True, output={"targets": targets})

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
    code = "window.location.href = " + json.dumps(url) + ";"
    result = self._execute_js(
      ControlCommand(
        command_id=command.command_id,
        target_kind="browser",
        action="execute_js",
        target_id=command.target_id,
        payload={"code": code},
        timeout_seconds=command.timeout_seconds,
      )
    )
    if not result.ok:
      return result
    output = dict(result.output)
    output["url"] = url
    return ControlResult(ok=True, output=output)

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

  def __init__(self, backend: ControlBackend) -> None:
    self._backend = backend

  def list_targets(self, kind: ControlTargetKind | None = None) -> list[ControlTarget]:
    return self._backend.list_targets(kind)

  async def inspect_browser(self, target_id: str | None = None) -> ControlResult:
    return await self._backend.execute(ControlCommand.create("browser", "inspect", target_id))

  async def execute_js(
    self,
    code: str,
    target_id: str | None = None,
    timeout_seconds: float | None = None,
  ) -> ControlResult:
    return await self._backend.execute(
      ControlCommand.create(
        "browser",
        "execute_js",
        target_id,
        {"code": code},
        timeout_seconds=timeout_seconds,
      )
    )

  async def navigate(
    self,
    url: str,
    target_id: str | None = None,
    timeout_seconds: float | None = None,
  ) -> ControlResult:
    return await self._backend.execute(
      ControlCommand.create(
        "browser",
        "navigate",
        target_id,
        {"url": url},
        timeout_seconds=timeout_seconds,
      )
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
