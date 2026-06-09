from datetime import timedelta
import json
import unittest

from agent_kernel.capabilities import CapabilityCallContext, CapabilityRegistry, CapabilityRuntime
from agent_kernel.capabilities.adapters import (
  ADBMobileBackend,
  BrowserLinkHTTPBackend,
  ControlCommand,
  ControlOwnerContext,
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

  async def test_browser_link_http_backend_create_tab_extracts_target_from_tabs_list_response(self) -> None:
    transport = _BrowserLinkTabsListTransport()
    backend = BrowserLinkHTTPBackend(post_json=transport.post_json, request_timeout_seconds=2)

    result = await backend.execute(
      ControlCommand.create("browser", "navigate", target_id=None, payload={"url": "https://meadow.example/explore"})
    )

    self.assertTrue(result.ok)
    self.assertEqual(result.output["target_id"], "tab_new")
    self.assertEqual(result.output["active_target_id"], "tab_new")
    self.assertEqual(result.output["target"]["metadata"]["url"], "https://meadow.example/explore")

  async def test_browser_link_http_backend_create_tab_confirms_delayed_session_match(self) -> None:
    transport = _BrowserLinkDelayedCreateTransport()
    backend = BrowserLinkHTTPBackend(post_json=transport.post_json, request_timeout_seconds=1)

    result = await backend.execute(
      ControlCommand.create("browser", "navigate", target_id=None, payload={"url": "https://meadow.example/explore"})
    )

    self.assertTrue(result.ok)
    self.assertEqual(result.output["target_id"], "tab_new")
    session_requests = [request for request in transport.requests if request.get("cmd") == "get_all_sessions"]
    self.assertGreaterEqual(len(session_requests), 2)

  async def test_browser_link_http_backend_create_tab_reuses_same_origin_target_when_create_returns_tabs(self) -> None:
    transport = _BrowserLinkSameOriginFallbackTransport()
    backend = BrowserLinkHTTPBackend(post_json=transport.post_json, request_timeout_seconds=1)

    result = await backend.execute(
      ControlCommand.create(
        "browser",
        "navigate",
        target_id=None,
        payload={"url": "https://www.google.com/search?q=immersive+translate"},
      )
    )

    self.assertTrue(result.ok)
    self.assertEqual(result.output["target_id"], "tab_google")
    self.assertTrue(result.output["reused_existing_target"])
    self.assertEqual(transport.requests[-1]["cmd"], "execute_js")
    self.assertEqual(transport.requests[-1]["sessionId"], "tab_google")
    self.assertIn("immersive+translate", transport.requests[-1]["code"])

  async def test_browser_link_http_backend_create_tab_does_not_claim_unmatched_existing_tab(self) -> None:
    transport = _BrowserLinkMismatchedTabsTransport()
    backend = BrowserLinkHTTPBackend(post_json=transport.post_json, request_timeout_seconds=2)

    result = await backend.execute(
      ControlCommand.create("browser", "navigate", target_id=None, payload={"url": "https://meadow.example/explore"})
    )

    self.assertFalse(result.ok)
    self.assertEqual(result.error["type"], "browser_target_creation_unconfirmed")
    self.assertNotIn("target_id", result.output)
    self.assertNotIn("active_target_id", result.output)
    self.assertEqual(result.output["url"], "https://meadow.example/explore")

  async def test_browser_link_http_backend_inspect_fetches_page_summary(self) -> None:
    transport = _BrowserLinkTransport()
    backend = BrowserLinkHTTPBackend(post_json=transport.post_json, request_timeout_seconds=2)

    result = await backend.execute(ControlCommand.create("browser", "inspect", target_id="tab_1"))

    self.assertTrue(result.ok)
    self.assertEqual(result.output["active_target_id"], "tab_1")
    self.assertEqual(result.output["page"]["title"], "Example Page")
    self.assertEqual(result.output["page"]["feed_titles"], ["推荐 A", "推荐 B"])
    self.assertEqual(result.output["page"]["links"][0]["text"], "结果 A")
    self.assertEqual(result.output["page"]["links"][0]["href"], "https://source.example/a")
    self.assertEqual(result.output["page"]["search_results"][0]["title"], "结果 A")

  async def test_browser_link_http_backend_inspect_tabs_only_skips_page_summary(self) -> None:
    transport = _BrowserLinkTransport()
    backend = BrowserLinkHTTPBackend(post_json=transport.post_json, request_timeout_seconds=2)

    result = await backend.execute(ControlCommand.create("browser", "inspect", payload={"tabs_only": True}))

    self.assertTrue(result.ok)
    self.assertIn("targets", result.output)
    self.assertNotIn("page", result.output)
    self.assertEqual([request["cmd"] for request in transport.requests], ["get_all_sessions"])

  async def test_control_workbench_browser_scope_can_see_all_tabs_without_claiming(self) -> None:
    backend = _ScopedBrowserBackend()
    workbench = ControlWorkbench(backend)

    result = await workbench.inspect_browser(payload={"tabs_only": True}, owner=ControlOwnerContext(run_id="run_daily", scope="chat"))

    self.assertTrue(result.ok)
    self.assertEqual([target["target_id"] for target in result.output["targets"]], ["tab_a", "tab_b"])
    self.assertNotIn("ownership", result.output["targets"][0])

  async def test_control_workbench_browser_scope_reuses_created_target_for_later_scan(self) -> None:
    backend = _ScopedBrowserBackend()
    owner = ControlOwnerContext(run_id="run_daily", scope="chat")
    workbench = ControlWorkbench(backend)

    navigated = await workbench.navigate("https://search.example", owner=owner)
    scanned = await workbench.inspect_browser(owner=owner)

    self.assertTrue(navigated.ok)
    self.assertEqual(navigated.output["target_id"], "tab_new")
    self.assertTrue(scanned.ok)
    self.assertEqual(backend.commands[-1].target_id, "tab_new")
    self.assertEqual(scanned.output["active_target_id"], "tab_new")
    self.assertEqual(scanned.output["browser_scope"]["active_target_id"], "tab_new")

  async def test_control_workbench_targetless_page_scan_returns_scope_error_without_random_page(self) -> None:
    backend = _ScopedBrowserBackend()
    workbench = ControlWorkbench(backend)
    owner = ControlOwnerContext(run_id="run_daily", agent_id="daily", scope="chat")

    scanned = await workbench.inspect_browser(owner=owner)

    self.assertTrue(scanned.ok)
    self.assertIn("targets", scanned.output)
    self.assertNotIn("page", scanned.output)
    self.assertEqual(scanned.output["page_error"]["type"], "browser_target_scope_required")
    self.assertEqual(len(backend.commands), 0)

  async def test_control_workbench_same_scope_can_continue_target_across_runs(self) -> None:
    backend = _ScopedBrowserBackend()
    workbench = ControlWorkbench(backend)
    first_turn = ControlOwnerContext(run_id="run_first", agent_id="daily", task_id="chat", scope="chat")
    next_turn = ControlOwnerContext(run_id="run_next", agent_id="daily", task_id="chat", scope="chat")

    navigated = await workbench.navigate("https://search.example", owner=first_turn)
    scanned = await workbench.inspect_browser(target_id=navigated.output["target_id"], owner=next_turn)

    self.assertTrue(navigated.ok)
    self.assertTrue(scanned.ok)
    self.assertEqual(scanned.output["active_target_id"], "tab_new")
    self.assertTrue(scanned.output["browser_scope"]["active_target_id"], "tab_new")
    targets = await workbench.inspect_browser(payload={"tabs_only": True}, owner=next_turn)
    owned = [target for target in targets.output["targets"] if target["target_id"] == "tab_new"][0]
    self.assertTrue(owned["owned_by_current_scope"])

  async def test_control_workbench_same_child_agent_can_continue_target_across_runs(self) -> None:
    backend = _ScopedBrowserBackend()
    workbench = ControlWorkbench(backend)
    first_turn = ControlOwnerContext(run_id="run_child_first", agent_id="child_a", task_id="slice_a", scope="chat")
    next_turn = ControlOwnerContext(run_id="run_child_next", agent_id="child_a", task_id="slice_a", scope="chat")

    navigated = await workbench.navigate("https://child.example", owner=first_turn)
    scanned = await workbench.inspect_browser(target_id=navigated.output["target_id"], owner=next_turn)

    self.assertTrue(navigated.ok)
    self.assertTrue(scanned.ok)
    self.assertEqual(scanned.output["active_target_id"], "tab_new")

  async def test_control_workbench_same_scope_rejects_different_child_agent_target_access(self) -> None:
    backend = _ScopedBrowserBackend()
    workbench = ControlWorkbench(backend)
    first_child = ControlOwnerContext(run_id="run_child_a", agent_id="child_a", task_id="slice_a", scope="chat")
    second_child = ControlOwnerContext(run_id="run_child_b", agent_id="child_b", task_id="slice_b", scope="chat")

    claimed = await workbench.navigate("https://owned.example", target_id="tab_a", owner=first_child)
    rejected = await workbench.inspect_browser(target_id="tab_a", owner=second_child)

    self.assertTrue(claimed.ok)
    self.assertFalse(rejected.ok)
    self.assertEqual(rejected.error["type"], "browser_target_owned_by_other_scope")

  async def test_control_workbench_browser_scope_rejects_other_owner_mutation(self) -> None:
    backend = _ScopedBrowserBackend()
    workbench = ControlWorkbench(backend)
    first_owner = ControlOwnerContext(run_id="run_a", scope="chat_a")
    second_owner = ControlOwnerContext(run_id="run_b", scope="chat_b")

    claimed = await workbench.navigate("https://owned.example", target_id="tab_a", owner=first_owner)
    rejected = await workbench.execute_js("document.title", target_id="tab_a", owner=second_owner)

    self.assertTrue(claimed.ok)
    self.assertFalse(rejected.ok)
    self.assertEqual(rejected.error["type"], "browser_target_owned_by_other_scope")

  async def test_control_workbench_subagent_scan_does_not_read_other_owner_page_by_default(self) -> None:
    backend = _ScopedBrowserBackend()
    workbench = ControlWorkbench(backend)
    first_owner = ControlOwnerContext(run_id="run_a", agent_id="agent_a", scope="chat_a")
    second_owner = ControlOwnerContext(run_id="run_b", agent_id="agent_b", scope="chat_b")

    await workbench.navigate("https://owned.example", target_id="tab_a", owner=first_owner)
    scanned = await workbench.inspect_browser(owner=second_owner)

    self.assertTrue(scanned.ok)
    self.assertIn("targets", scanned.output)
    self.assertNotIn("page", scanned.output)
    self.assertEqual(scanned.output["page_error"]["type"], "browser_target_scope_required")

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


class _ScopedBrowserBackend:
  def __init__(self) -> None:
    self.commands: list[ControlCommand] = []
    self.targets = {
      "tab_a": ControlTarget(target_id="tab_a", kind="browser", label="A", metadata={"url": "https://a.example"}),
      "tab_b": ControlTarget(target_id="tab_b", kind="browser", label="B", metadata={"url": "https://b.example"}),
    }

  def list_targets(self, kind=None):
    if kind not in (None, "browser"):
      return []
    return list(self.targets.values())

  async def execute(self, command):
    self.commands.append(command)
    if command.action == "navigate":
      url = command.payload.get("url")
      target_id = command.target_id or "tab_new"
      self.targets[target_id] = ControlTarget(
        target_id=target_id,
        kind="browser",
        label="New",
        metadata={"url": url},
      )
      return ControlResult(ok=True, output={"target_id": target_id, "active_target_id": target_id, "url": url})
    if command.action == "inspect":
      output = {"targets": [target.to_dict() for target in self.list_targets("browser")]}
      if command.payload.get("tabs_only"):
        return ControlResult(ok=True, output=output)
      target_id = command.target_id or "tab_a"
      output["active_target_id"] = target_id
      output["page"] = {"title": target_id, "url": self.targets[target_id].metadata.get("url")}
      return ControlResult(ok=True, output=output)
    if command.action == "execute_js":
      return ControlResult(ok=True, output={"target_id": command.target_id, "result": {"data": "ok"}})
    return ControlResult(ok=False, error={"type": "unsupported"})


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
                "links": [{"text": "结果 A", "href": "https://source.example/a"}],
                "search_results": [
                  {"title": "结果 A", "href": "https://source.example/a", "snippet": "来源 A 摘要"}
                ],
                "text": "推荐 A 推荐 B",
              }
            }
          }
      return {"r": {"data": "Example"}}
    return {"r": {"error": "unsupported"}}


class _BrowserLinkTabsListTransport:
  def __init__(self) -> None:
    self.requests: list[dict[str, object]] = []

  def post_json(self, payload: dict[str, object]) -> dict[str, object]:
    self.requests.append(payload)
    if payload.get("cmd") == "get_all_sessions":
      return {
        "r": [
          {"id": "tab_old", "url": "https://old.example", "title": "Old", "type": "ext_ws"},
          {"id": "tab_new", "url": "https://meadow.example/explore", "title": "Created", "type": "ext_ws"},
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
          return {
            "r": {
              "data": [
                {"id": "tab_old", "url": "https://old.example", "title": "Old", "active": False},
                {"id": "tab_new", "url": command["url"], "title": "Created", "active": True},
              ]
            }
          }
      return {"r": {"data": "ok"}}
    return {"r": {"error": "unsupported"}}


class _BrowserLinkDelayedCreateTransport:
  def __init__(self) -> None:
    self.requests: list[dict[str, object]] = []
    self._session_reads = 0

  def post_json(self, payload: dict[str, object]) -> dict[str, object]:
    self.requests.append(payload)
    if payload.get("cmd") == "get_all_sessions":
      self._session_reads += 1
      if self._session_reads == 1:
        return {"r": [{"id": "tab_old", "url": "https://old.example", "title": "Old", "type": "ext_ws"}]}
      return {
        "r": [
          {"id": "tab_old", "url": "https://old.example", "title": "Old", "type": "ext_ws"},
          {"id": "tab_new", "url": "https://meadow.example/explore", "title": "Created", "type": "ext_ws"},
        ]
      }
    if payload.get("cmd") == "execute_js":
      return {"r": {"data": "accepted"}}
    return {"r": {"error": "unsupported"}}


class _BrowserLinkSameOriginFallbackTransport:
  def __init__(self) -> None:
    self.requests: list[dict[str, object]] = []

  def post_json(self, payload: dict[str, object]) -> dict[str, object]:
    self.requests.append(payload)
    if payload.get("cmd") == "get_all_sessions":
      return {
        "r": [
          {"id": "tab_old", "url": "https://old.example", "title": "Old", "type": "ext_ws"},
          {"id": "tab_google", "url": "https://www.google.com/search?q=old", "title": "Google", "type": "ext_ws"},
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
          return {
            "r": {
              "data": [
                {"id": "tab_old", "url": "https://old.example", "title": "Old", "active": False},
                {"id": "tab_google", "url": "https://www.google.com/search?q=old", "title": "Google", "active": True},
              ]
            }
          }
      return {"r": {"data": "ok"}}
    return {"r": {"error": "unsupported"}}


class _BrowserLinkMismatchedTabsTransport:
  def __init__(self) -> None:
    self.requests: list[dict[str, object]] = []

  def post_json(self, payload: dict[str, object]) -> dict[str, object]:
    self.requests.append(payload)
    if payload.get("cmd") == "get_all_sessions":
      return {
        "r": [
          {"id": "tab_old", "url": "https://old.example", "title": "Old", "type": "ext_ws"},
          {"id": "tab_invoice", "url": "https://bestvm.cloud/viewinvoice.php?id=1", "title": "Invoice", "type": "ext_ws"},
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
          return {
            "r": {
              "data": [
                {"id": "tab_old", "url": "https://old.example", "title": "Old", "active": False},
                {"id": "tab_invoice", "url": "https://bestvm.cloud/viewinvoice.php?id=1", "title": "Invoice", "active": True},
              ]
            }
          }
      return {"r": {"data": "ok"}}
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
