from datetime import timedelta
import json
import unittest

from agent_kernel.capabilities import CapabilityCallContext, CapabilityRegistry, CapabilityRuntime
from agent_kernel.capabilities.adapters import (
  ADBMobileBackend,
  BrowserLinkHTTPBackend,
  ControlCommand,
  CommandResult,
  ControlResult,
  ControlTarget,
  ControlWorkbench,
  DriverVisionDetector,
  FakeControlBackend,
  HTTPVisionDetector,
  HTTPVisionEndpoint,
  LocalToolExecutor,
  UIAutomationDesktopDetector,
  UIAStyleDesktopDetector,
  Win32DesktopBackend,
)
from agent_kernel.domain.base import utc_now
from agent_kernel.domain.capability import CapabilityGrant, CapabilitySpec, SideEffectLevel
from agent_kernel.persistence import UnitOfWork, connect_sqlite
from agent_kernel.policy import PolicyDecisionType, PolicyEngine
from agent_kernel.runtime import unit_of_work_factory


class ControlWorkbenchTests(unittest.IsolatedAsyncioTestCase):
  async def test_fake_control_backend_executes_browser_js(self) -> None:
    backend = FakeControlBackend()
    backend.register_target(
      ControlTarget(
        target_id="tab_1",
        kind="browser",
        label="reference browser tab",
        metadata={"url": "https://example.test"},
      )
    )
    backend.register_response(
      "browser",
      "execute_js",
      ControlResult(ok=True, output={"data": {"title": "Example"}}),
    )
    workbench = ControlWorkbench(backend)

    result = await workbench.execute_js("document.title", target_id="tab_1", timeout_seconds=2)

    self.assertTrue(result.ok)
    self.assertEqual(result.output["data"], {"title": "Example"})
    self.assertEqual(backend.commands[0].target_kind, "browser")
    self.assertEqual(backend.commands[0].action, "execute_js")
    self.assertEqual(backend.commands[0].payload["code"], "document.title")

  async def test_control_workbench_exposes_desktop_and_mobile_atoms(self) -> None:
    backend = FakeControlBackend()
    backend.register_target(ControlTarget(target_id="window_1", kind="desktop"))
    backend.register_target(ControlTarget(target_id="device_1", kind="mobile"))
    backend.register_response("desktop", "click", ControlResult(ok=True, output={"changed": True}))
    backend.register_response(
      "mobile",
      "dump_ui",
      ControlResult(ok=True, output={"nodes": [{"text": "Pay", "cx": 10, "cy": 20}]}),
    )
    workbench = ControlWorkbench(backend)

    click = await workbench.click("desktop", 100, 200, target_id="window_1")
    ui = await workbench.dump_ui("mobile", target_id="device_1")

    self.assertTrue(click.ok)
    self.assertTrue(ui.ok)
    self.assertEqual(backend.commands[0].payload, {"x": 100, "y": 200})
    self.assertEqual(ui.output["nodes"][0]["text"], "Pay")

  async def test_capability_runtime_routes_workbench_through_policy_audit_and_tool_call(self) -> None:
    conn = connect_sqlite()
    try:
      registry = CapabilityRegistry()
      registry.register(
        CapabilitySpec(
          capability_id="workbench.control",
          name="control",
          kind="workbench",
          input_schema={},
          output_schema={},
          side_effect_level=SideEffectLevel.EXTERNAL_MUTATION,
        )
      )
      grant = CapabilityGrant(
        grant_id="grant_1",
        capability_id="workbench.control",
        run_id="run_control",
        expires_at=utc_now() + timedelta(minutes=5),
      )
      backend = FakeControlBackend()
      backend.register_target(ControlTarget(target_id="tab_1", kind="browser"))
      backend.register_response("browser", "navigate", ControlResult(ok=True, output={"url": "https://example.test"}))
      runtime = CapabilityRuntime(
        registry,
        PolicyEngine(grants=[grant]),
        LocalToolExecutor(),
        control_workbench=ControlWorkbench(backend),
        uow_factory=unit_of_work_factory(conn),
      )

      outcome = await runtime.call(
        "workbench.control",
        {
          "action": "navigate",
          "target_kind": "browser",
          "target_id": "tab_1",
          "payload": {"url": "https://example.test"},
        },
        CapabilityCallContext(run_id="run_control", agent_id="agent_1"),
      )

      with UnitOfWork(conn) as uow:
        calls = uow.tool_calls.list_by_run("run_control")
        audits = uow.audit.list_by_run("run_control")

      self.assertTrue(outcome.result.ok)
      self.assertEqual(outcome.decision.type, PolicyDecisionType.ALLOW)
      self.assertEqual(calls[0].capability_id, "workbench.control")
      self.assertEqual(calls[0].status, "succeeded")
      self.assertEqual(audits[0].action, "capability.call.policy_check")
      self.assertEqual(backend.commands[0].action, "navigate")
    finally:
      conn.close()

  async def test_high_risk_control_requires_approval_without_grant(self) -> None:
    registry = CapabilityRegistry()
    registry.register(
      CapabilitySpec(
        capability_id="workbench.control",
        name="control",
        kind="workbench",
        input_schema={},
        output_schema={},
        side_effect_level=SideEffectLevel.EXTERNAL_MUTATION,
      )
    )
    backend = FakeControlBackend()
    runtime = CapabilityRuntime(
      registry,
      PolicyEngine(),
      LocalToolExecutor(),
      control_workbench=ControlWorkbench(backend),
    )

    outcome = await runtime.call(
      "workbench.control",
      {"action": "click", "target_kind": "desktop", "payload": {"x": 1, "y": 2}},
      CapabilityCallContext(run_id="run_approval"),
    )

    self.assertTrue(outcome.requires_approval)
    self.assertEqual(backend.commands, [])

  async def test_missing_control_workbench_returns_standard_tool_result(self) -> None:
    registry = CapabilityRegistry()
    registry.register(
      CapabilitySpec(
        capability_id="workbench.control",
        name="control",
        kind="workbench",
        input_schema={},
        output_schema={},
        side_effect_level=SideEffectLevel.NONE,
      )
    )
    runtime = CapabilityRuntime(registry, PolicyEngine(), LocalToolExecutor())

    outcome = await runtime.call(
      "workbench.control",
      {"action": "inspect_browser"},
      CapabilityCallContext(run_id="run_missing"),
    )

    self.assertFalse(outcome.result.ok)
    self.assertEqual(outcome.result.error["type"], "control_workbench_not_configured")

  async def test_browser_link_http_backend_lists_targets_and_executes_js(self) -> None:
    transport = _BrowserLinkTransport()
    backend = BrowserLinkHTTPBackend(post_json=transport.post_json, request_timeout_seconds=2)

    targets = backend.list_targets("browser")
    result = await backend.execute(
      ControlTargetCommandFactory.execute_js("tab_1", "document.title")
    )

    self.assertEqual(targets[0].target_id, "tab_1")
    self.assertEqual(targets[0].metadata["url"], "https://example.test")
    self.assertTrue(result.ok)
    self.assertEqual(result.output["result"], {"data": "Example"})
    self.assertEqual(transport.requests[-1]["cmd"], "execute_js")
    self.assertEqual(transport.requests[-1]["sessionId"], "tab_1")

  async def test_browser_link_http_backend_navigate_existing_target_uses_execute_js_bridge_command(self) -> None:
    transport = _BrowserLinkTransport()
    backend = BrowserLinkHTTPBackend(post_json=transport.post_json, request_timeout_seconds=2)

    result = await backend.execute(
      ControlTargetCommandFactory.navigate("tab_1", "https://meadow.example")
    )

    self.assertTrue(result.ok)
    self.assertEqual(result.output["url"], "https://meadow.example")
    self.assertEqual(transport.requests[-1]["cmd"], "execute_js")
    self.assertIn("window.location.href", transport.requests[-1]["code"])
    self.assertIn("https://meadow.example", transport.requests[-1]["code"])

  async def test_browser_link_http_backend_navigate_without_target_creates_tab_via_extension(self) -> None:
    transport = _BrowserLinkTransport()
    backend = BrowserLinkHTTPBackend(post_json=transport.post_json, request_timeout_seconds=2)

    result = await backend.execute(
      ControlCommand.create("browser", "navigate", target_id=None, payload={"url": "https://meadow.example"})
    )

    self.assertTrue(result.ok)
    self.assertEqual(result.output["target_id"], "tab_2")
    self.assertEqual(transport.requests[-1]["cmd"], "execute_js")
    command = json.loads(transport.requests[-1]["code"])
    self.assertEqual(command["cmd"], "tabs")
    self.assertEqual(command["method"], "create")
    self.assertEqual(command["url"], "https://meadow.example")

  async def test_browser_link_http_backend_inspect_fetches_page_summary(self) -> None:
    transport = _BrowserLinkTransport()
    backend = BrowserLinkHTTPBackend(post_json=transport.post_json, request_timeout_seconds=2)

    result = await backend.execute(ControlCommand.create("browser", "inspect", target_id="tab_1"))

    self.assertTrue(result.ok)
    self.assertEqual(result.output["active_target_id"], "tab_1")
    self.assertEqual(result.output["page"]["title"], "Example Page")
    self.assertEqual(result.output["page"]["feed_titles"], ["推荐 A", "推荐 B"])

  async def test_browser_link_http_backend_inspect_tabs_only_skips_page_summary(self) -> None:
    transport = _BrowserLinkTransport()
    backend = BrowserLinkHTTPBackend(post_json=transport.post_json, request_timeout_seconds=2)

    result = await backend.execute(ControlCommand.create("browser", "inspect", payload={"tabs_only": True}))

    self.assertTrue(result.ok)
    self.assertIn("targets", result.output)
    self.assertNotIn("page", result.output)
    self.assertEqual([request["cmd"] for request in transport.requests], ["get_all_sessions"])

  async def test_adb_mobile_backend_lists_devices_and_parses_ui_dump(self) -> None:
    runner = _ADBRunner()
    backend = ADBMobileBackend(adb_path="adb", runner=runner.run, vision_detector=DriverVisionDetector(_VisionDriver()))

    targets = backend.list_targets("mobile")
    result = await backend.execute(ControlTargetCommandFactory.dump_mobile_ui("device_1"))

    self.assertEqual(targets[0].target_id, "device_1")
    self.assertTrue(result.ok)
    self.assertEqual(result.output["nodes"][0]["text"], "Pay")
    self.assertEqual(result.output["nodes"][0]["cx"], 20)
    self.assertEqual(result.output["nodes"][0]["cy"], 30)
    self.assertEqual(result.output["nodes"][1]["source"], "vision")
    self.assertEqual(result.output["nodes"][1]["text"], "Checkout")
    self.assertIn(["adb", "-s", "device_1", "shell", "uiautomator", "dump", "--compressed", "/sdcard/window.xml"], runner.calls)

  async def test_adb_mobile_backend_tap_text_key_and_screenshot(self) -> None:
    runner = _ADBRunner()
    backend = ADBMobileBackend(adb_path="adb", runner=runner.run)

    tap = await backend.execute(ControlTargetCommandFactory.tap_mobile("device_1", 10, 20))
    typed = await backend.execute(ControlTargetCommandFactory.type_mobile("device_1", "hello world"))
    key = await backend.execute(ControlTargetCommandFactory.key_mobile("device_1", "ENTER"))
    screenshot = await backend.execute(ControlTargetCommandFactory.mobile_screenshot("device_1"))

    self.assertTrue(tap.ok)
    self.assertTrue(typed.ok)
    self.assertTrue(key.ok)
    self.assertTrue(screenshot.ok)
    self.assertEqual(screenshot.output["media_type"], "image/png")
    self.assertEqual(runner.calls[-4], ["adb", "-s", "device_1", "shell", "input", "tap", "10", "20"])
    self.assertEqual(runner.calls[-3], ["adb", "-s", "device_1", "shell", "input", "text", "hello%sworld"])
    self.assertEqual(runner.calls[-2], ["adb", "-s", "device_1", "shell", "input", "keyevent", "ENTER"])
    self.assertEqual(runner.calls[-1], ["adb", "-s", "device_1", "exec-out", "screencap", "-p"])

  async def test_win32_desktop_backend_lists_windows_and_controls_driver(self) -> None:
    driver = _DesktopDriver()
    clipboard = _Clipboard()
    backend = Win32DesktopBackend(
      desktop_driver=driver,
      win32gui=_Win32Gui(),
      clipboard=clipboard,
    )

    targets = backend.list_targets("desktop")
    screenshot = await backend.execute(ControlTargetCommandFactory.desktop_screenshot("101"))
    click = await backend.execute(ControlTargetCommandFactory.desktop_click("101", 20, 30))
    key = await backend.execute(ControlTargetCommandFactory.desktop_key("101", "ctrl+c"))
    typed = await backend.execute(ControlTargetCommandFactory.desktop_type("101", "hello desktop"))

    self.assertEqual(targets[0].target_id, "101")
    self.assertEqual(targets[0].label, "Editor")
    self.assertTrue(screenshot.ok)
    self.assertEqual(screenshot.output["media_type"], "image/png")
    self.assertTrue(click.ok)
    self.assertTrue(key.ok)
    self.assertTrue(typed.ok)
    self.assertEqual(driver.activations, [101, 101, 101])
    self.assertEqual(driver.clicks, [(20, 30)])
    self.assertEqual(driver.presses, ["ctrl+c", "ctrl+v"])
    self.assertEqual(clipboard.values, ["hello desktop"])

  async def test_win32_desktop_backend_returns_standard_error_without_driver(self) -> None:
    backend = Win32DesktopBackend(win32gui=_Win32Gui())

    result = await backend.execute(ControlTargetCommandFactory.desktop_click("101", 20, 30))

    self.assertFalse(result.ok)
    self.assertEqual(result.error["type"], "desktop_driver_not_available")

  async def test_win32_desktop_backend_dumps_uia_style_tree(self) -> None:
    backend = Win32DesktopBackend(
      win32gui=_Win32Gui(),
      ui_detector=UIAStyleDesktopDetector(_UIADriver()),
      desktop_driver=_DesktopDriver(),
      vision_detector=DriverVisionDetector(_VisionDriver()),
    )

    result = await backend.execute(ControlTargetCommandFactory.desktop_dump_ui("101"))

    self.assertTrue(result.ok)
    self.assertEqual(result.output["nodes"][0]["text"], "Save")
    self.assertEqual(result.output["nodes"][0]["control_type"], "Button")
    self.assertEqual(result.output["nodes"][0]["automation_id"], "save")
    self.assertEqual(result.output["nodes"][0]["cx"], 15)
    self.assertEqual(result.output["nodes"][0]["cy"], 25)
    self.assertEqual(result.output["nodes"][1]["source"], "vision")
    self.assertEqual(result.output["nodes"][1]["label"], "Checkout")

  async def test_uiautomation_desktop_detector_dumps_real_provider_shape(self) -> None:
    backend = Win32DesktopBackend(
      win32gui=_Win32Gui(),
      ui_detector=UIAutomationDesktopDetector(_UIAutomationModule()),
      desktop_driver=_DesktopDriver(),
    )

    result = await backend.execute(ControlTargetCommandFactory.desktop_dump_ui("101"))

    self.assertTrue(result.ok)
    self.assertEqual(result.output["nodes"][0]["text"], "Editor")
    self.assertEqual(result.output["nodes"][0]["control_type"], "Window")
    self.assertEqual(result.output["nodes"][0]["automation_id"], "editor")
    self.assertEqual(result.output["nodes"][0]["bounds"], [0, 0, 200, 100])
    self.assertEqual(result.output["nodes"][1]["text"], "Save")
    self.assertEqual(result.output["nodes"][1]["cx"], 15)
    self.assertEqual(result.output["nodes"][1]["cy"], 25)

  async def test_win32_desktop_backend_can_dump_vision_nodes_without_uia_detector(self) -> None:
    backend = Win32DesktopBackend(
      win32gui=_Win32Gui(),
      desktop_driver=_DesktopDriver(),
      vision_detector=DriverVisionDetector(_VisionDriver()),
    )

    result = await backend.execute(ControlTargetCommandFactory.desktop_dump_ui("101"))

    self.assertTrue(result.ok)
    self.assertEqual(result.output["nodes"][0]["source"], "vision")
    self.assertEqual(result.output["nodes"][0]["cx"], 20)
    self.assertEqual(result.output["nodes"][0]["cy"], 30)

  async def test_http_vision_detector_normalizes_service_detections(self) -> None:
    transport = _VisionHTTPTransport()
    detector = HTTPVisionDetector(
      HTTPVisionEndpoint(
        url="https://vision.example/detect",
        headers={"Authorization": "Bearer test"},
      ),
      post_json=transport.post_json,
    )

    nodes = detector.detect(b"\x89PNG\r\n", "desktop", "101")

    self.assertEqual(nodes[0]["source"], "vision")
    self.assertEqual(nodes[0]["label"], "Submit")
    self.assertEqual(nodes[0]["text"], "Submit")
    self.assertEqual(nodes[0]["confidence"], 0.91)
    self.assertEqual(nodes[0]["bounds"], [10, 20, 30, 40])
    self.assertEqual(nodes[0]["cx"], 20)
    self.assertEqual(nodes[0]["cy"], 30)
    self.assertEqual(transport.requests[0]["target_kind"], "desktop")
    self.assertEqual(transport.requests[0]["target_id"], "101")
    self.assertEqual(transport.requests[0]["image"]["media_type"], "image/png")

  async def test_http_vision_detector_rejects_invalid_service_envelope(self) -> None:
    detector = HTTPVisionDetector("https://vision.example/detect", post_json=lambda _payload: {"ok": True})

    with self.assertRaisesRegex(RuntimeError, "detections"):
      detector.detect(b"\x89PNG\r\n", "mobile", "device_1")

  async def test_mobile_dump_ui_accepts_http_vision_detector(self) -> None:
    runner = _ADBRunner()
    backend = ADBMobileBackend(
      adb_path="adb",
      runner=runner.run,
      vision_detector=HTTPVisionDetector("https://vision.example/detect", post_json=_VisionHTTPTransport().post_json),
    )

    result = await backend.execute(ControlTargetCommandFactory.dump_mobile_ui("device_1"))

    self.assertTrue(result.ok)
    self.assertEqual(result.output["nodes"][1]["source"], "vision")
    self.assertEqual(result.output["nodes"][1]["label"], "Submit")


class ControlTargetCommandFactory:
  @staticmethod
  def execute_js(target_id: str, code: str):
    from agent_kernel.capabilities.adapters import ControlCommand

    return ControlCommand.create(
      "browser",
      "execute_js",
      target_id=target_id,
      payload={"code": code},
      timeout_seconds=2,
    )

  @staticmethod
  def navigate(target_id: str, url: str):
    from agent_kernel.capabilities.adapters import ControlCommand

    return ControlCommand.create(
      "browser",
      "navigate",
      target_id=target_id,
      payload={"url": url},
      timeout_seconds=2,
    )

  @staticmethod
  def dump_mobile_ui(target_id: str):
    from agent_kernel.capabilities.adapters import ControlCommand

    return ControlCommand.create("mobile", "dump_ui", target_id=target_id, timeout_seconds=2)

  @staticmethod
  def tap_mobile(target_id: str, x: int, y: int):
    from agent_kernel.capabilities.adapters import ControlCommand

    return ControlCommand.create(
      "mobile",
      "tap",
      target_id=target_id,
      payload={"x": x, "y": y},
      timeout_seconds=2,
    )

  @staticmethod
  def type_mobile(target_id: str, text: str):
    from agent_kernel.capabilities.adapters import ControlCommand

    return ControlCommand.create(
      "mobile",
      "type_text",
      target_id=target_id,
      payload={"text": text},
      timeout_seconds=2,
    )

  @staticmethod
  def key_mobile(target_id: str, key: str):
    from agent_kernel.capabilities.adapters import ControlCommand

    return ControlCommand.create(
      "mobile",
      "key",
      target_id=target_id,
      payload={"key": key},
      timeout_seconds=2,
    )

  @staticmethod
  def mobile_screenshot(target_id: str):
    from agent_kernel.capabilities.adapters import ControlCommand

    return ControlCommand.create("mobile", "screenshot", target_id=target_id, timeout_seconds=2)

  @staticmethod
  def desktop_screenshot(target_id: str):
    from agent_kernel.capabilities.adapters import ControlCommand

    return ControlCommand.create("desktop", "screenshot", target_id=target_id, timeout_seconds=2)

  @staticmethod
  def desktop_click(target_id: str, x: int, y: int):
    from agent_kernel.capabilities.adapters import ControlCommand

    return ControlCommand.create(
      "desktop",
      "click",
      target_id=target_id,
      payload={"x": x, "y": y},
      timeout_seconds=2,
    )

  @staticmethod
  def desktop_key(target_id: str, key: str):
    from agent_kernel.capabilities.adapters import ControlCommand

    return ControlCommand.create(
      "desktop",
      "key",
      target_id=target_id,
      payload={"key": key},
      timeout_seconds=2,
    )

  @staticmethod
  def desktop_type(target_id: str, text: str):
    from agent_kernel.capabilities.adapters import ControlCommand

    return ControlCommand.create(
      "desktop",
      "type_text",
      target_id=target_id,
      payload={"text": text},
      timeout_seconds=2,
    )

  @staticmethod
  def desktop_dump_ui(target_id: str):
    from agent_kernel.capabilities.adapters import ControlCommand

    return ControlCommand.create("desktop", "dump_ui", target_id=target_id, timeout_seconds=2)


class _BrowserLinkTransport:
  def __init__(self) -> None:
    self.requests: list[dict[str, object]] = []

  def post_json(self, payload: dict[str, object]) -> dict[str, object]:
    self.requests.append(payload)
    if payload.get("cmd") == "get_all_sessions":
      return {
        "r": [
          {
            "id": "tab_1",
            "url": "https://example.test",
            "title": "Example",
            "type": "ext_ws",
          }
        ]
      }
    if payload.get("cmd") == "find_session":
      return {
        "r": [
          [
            "tab_1",
            {
              "url": "https://example.test",
              "title": "Example",
              "type": "ext_ws",
            },
          ]
        ]
      }
    if payload.get("cmd") == "execute_js":
      code = payload.get("code")
      if isinstance(code, str):
        try:
          command = json.loads(code)
        except json.JSONDecodeError:
          command = None
        if isinstance(command, dict) and command.get("cmd") == "tabs" and command.get("method") == "create":
          return {"r": {"data": {"id": "tab_2", "url": command["url"], "title": "Created"}}}
        if "feed_titles" in code:
          return {
            "r": {
              "data": {
                "url": "https://example.test",
                "title": "Example Page",
                "feed_titles": ["推荐 A", "推荐 B"],
                "visible_cards": [],
                "text": "推荐 A 推荐 B",
              }
            }
          }
      return {"r": {"data": "Example"}}
    return {"r": {"error": "unsupported"}}


class _VisionHTTPTransport:
  def __init__(self) -> None:
    self.requests: list[dict[str, object]] = []

  def post_json(self, payload: dict[str, object]) -> dict[str, object]:
    self.requests.append(payload)
    return {
      "detections": [
        {
          "label": "Submit",
          "confidence": 0.91,
          "bounds": [10, 20, 30, 40],
          "clickable": True,
        }
      ]
    }


class _ADBRunner:
  def __init__(self) -> None:
    self.calls: list[list[str]] = []

  def run(self, argv: list[str], timeout_seconds: float | None) -> CommandResult:
    self.calls.append(argv)
    if argv == ["adb", "devices"]:
      return CommandResult(returncode=0, stdout="List of devices attached\ndevice_1\tdevice\n")
    if argv[-5:] == ["shell", "uiautomator", "dump", "--compressed", "/sdcard/window.xml"]:
      return CommandResult(returncode=0, stdout="UI hierchary dumped to: /sdcard/window.xml")
    if argv[-3:] == ["shell", "cat", "/sdcard/window.xml"]:
      return CommandResult(
        returncode=0,
        stdout=(
          '<?xml version="1.0" encoding="UTF-8"?>'
          '<hierarchy>'
          '<node text="Pay" content-desc="" clickable="true" class="android.widget.Button" '
          'resource-id="com.example:id/pay" bounds="[10,20][30,40]" />'
          '</hierarchy>'
        ),
      )
    if argv[-3:-2] == ["tap"] or argv[-3:-2] == ["text"] or argv[-2:-1] == ["keyevent"]:
      return CommandResult(returncode=0, stdout="")
    if argv[-3:] == ["screencap", "-p"]:
      return CommandResult(returncode=0, stdout_bytes=b"\x89PNG\r\n")
    return CommandResult(returncode=0, stdout="")


class _Win32Gui:
  def EnumWindows(self, callback, extra) -> None:
    callback(101, extra)
    callback(102, extra)

  def IsWindowVisible(self, hwnd: int) -> bool:
    return hwnd == 101

  def GetWindowText(self, hwnd: int) -> str:
    return "Editor" if hwnd == 101 else ""

  def GetWindowRect(self, hwnd: int) -> tuple[int, int, int, int]:
    return (10, 20, 300, 400)

  def GetClassName(self, hwnd: int) -> str:
    return "Notepad"


class _DesktopImage:
  def save(self, buffer, format: str) -> None:
    buffer.write(b"\x89PNG\r\n")


class _DesktopDriver:
  def __init__(self) -> None:
    self.activations: list[int] = []
    self.clicks: list[tuple[int, int]] = []
    self.presses: list[str] = []

  def Activate(self, target: int | str) -> None:
    self.activations.append(target)

  def GrabWindow(self, target: int | str) -> _DesktopImage:
    return _DesktopImage()

  def Click(self, x: int, y: int) -> dict[str, object]:
    self.clicks.append((x, y))
    return {"changed": True}

  def Press(self, key: str) -> str:
    self.presses.append(key)
    return "ok"


class _Clipboard:
  def __init__(self) -> None:
    self.values: list[str] = []

  def copy(self, text: str) -> None:
    self.values.append(text)


class _UIADriver:
  def dump_tree(self, target: int | str) -> list[dict[str, object]]:
    return [
      {
        "name": "Save",
        "control_type": "Button",
        "automation_id": "save",
        "class_name": "Button",
        "clickable": True,
        "enabled": True,
        "bounds": [10, 20, 20, 30],
      }
    ]


class _UIARect:
  def __init__(self, left: int, top: int, right: int, bottom: int) -> None:
    self.left = left
    self.top = top
    self.right = right
    self.bottom = bottom


class _UIAControl:
  def __init__(
    self,
    name: str,
    control_type: str,
    automation_id: str,
    rect: _UIARect,
    children: list["_UIAControl"] | None = None,
  ) -> None:
    self.Name = name
    self.ControlTypeName = control_type
    self.AutomationId = automation_id
    self.ClassName = control_type
    self.BoundingRectangle = rect
    self.IsEnabled = True
    self._children = children or []

  def GetChildren(self) -> list["_UIAControl"]:
    return self._children


class _UIAutomationModule:
  def __init__(self) -> None:
    self.root = _UIAControl(
      "Editor",
      "Window",
      "editor",
      _UIARect(0, 0, 200, 100),
      [
        _UIAControl(
          "Save",
          "Button",
          "save",
          _UIARect(10, 20, 20, 30),
        )
      ],
    )
    self.handles: list[int] = []

  def ControlFromHandle(self, hwnd: int) -> _UIAControl:
    self.handles.append(hwnd)
    return self.root


class _VisionDriver:
  def detect(self, image_bytes: bytes, target_kind: str, target_id: str | None = None) -> list[dict[str, object]]:
    return [
      {
        "label": "Checkout",
        "text": "Checkout",
        "confidence": 0.91,
        "bounds": [10, 20, 30, 40],
        "clickable": True,
      }
    ]


if __name__ == "__main__":
  unittest.main()
