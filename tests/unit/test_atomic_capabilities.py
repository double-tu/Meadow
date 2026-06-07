from datetime import timedelta
from pathlib import Path
import tempfile
import unittest

from agent_kernel.capabilities import (
  AtomicCapabilityProvider,
  AtomicCapabilityIds,
  CapabilityCallContext,
  CapabilityRegistry,
  CapabilityRuntime,
)
from agent_kernel.capabilities.adapters import (
  ControlResult,
  ControlTarget,
  ControlWorkbench,
  FakeControlBackend,
  HTTPResponse,
  LocalFileWorkspace,
  LocalToolExecutor,
)
from agent_kernel.domain import CapabilityGrant
from agent_kernel.domain.base import utc_now
from agent_kernel.policy import PolicyEngine


class AtomicCapabilityTests(unittest.IsolatedAsyncioTestCase):
  async def test_workspace_read_supports_keyword_context_and_line_numbers(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / "notes.txt"
      path.write_text("alpha\nbeta\ngamma\ndelta\n", encoding="utf-8")
      provider = AtomicCapabilityProvider(file_workspace=LocalFileWorkspace([tmp]))

      result = provider.read_workspace(
        {
          "path": str(path),
          "keyword": "gamma",
          "count": 2,
          "show_line_numbers": True,
        }
      )

      self.assertTrue(result.ok)
      self.assertEqual(result.output["content"], "2|beta\n3|gamma")
      self.assertEqual(result.output["start"], 2)
      self.assertTrue(result.output["matched"])

  async def test_http_request_uses_network_policy_and_adapter(self) -> None:
    registry = CapabilityRegistry()
    local_tools = LocalToolExecutor()
    client = _RecordingHTTPClient()
    provider = AtomicCapabilityProvider(http_client=client)
    provider.register(registry, local_tools)
    runtime = CapabilityRuntime(
      registry,
      PolicyEngine(
        grants=[
          CapabilityGrant(
            grant_id="grant_http",
            capability_id=AtomicCapabilityIds.HTTP_REQUEST,
            run_id="run_http",
            expires_at=utc_now() + timedelta(minutes=5),
            network_scope=["api.example.test"],
          )
        ]
      ),
      local_tools,
    )

    allowed = await runtime.call(
      AtomicCapabilityIds.HTTP_REQUEST,
      {"url": "https://api.example.test/status", "method": "GET"},
      CapabilityCallContext(run_id="run_http"),
    )
    denied = await runtime.call(
      AtomicCapabilityIds.HTTP_REQUEST,
      {"url": "https://other.example.test/status", "method": "GET"},
      CapabilityCallContext(run_id="run_http"),
    )

    self.assertTrue(allowed.result.ok)
    self.assertEqual(allowed.result.output["body"], "ok")
    self.assertEqual(client.requests[0][1], "https://api.example.test/status")
    self.assertFalse(denied.result.ok)
    self.assertEqual(denied.result.error["type"], "policy_denied")
    self.assertEqual(len(client.requests), 1)

  async def test_atomic_desktop_and_mobile_actions_route_to_control_workbench(self) -> None:
    registry = CapabilityRegistry()
    local_tools = LocalToolExecutor()
    provider = AtomicCapabilityProvider()
    provider.register(registry, local_tools)
    backend = FakeControlBackend()
    backend.register_target(ControlTarget(target_id="window_1", kind="desktop"))
    backend.register_target(ControlTarget(target_id="device_1", kind="mobile"))
    backend.register_response("desktop", "click", ControlResult(ok=True, output={"clicked": True}))
    backend.register_response("mobile", "dump_ui", ControlResult(ok=True, output={"nodes": [{"text": "Pay"}]}))
    runtime = CapabilityRuntime(
      registry,
      PolicyEngine(
        grants=[
          CapabilityGrant(
            grant_id="grant_desktop",
            capability_id=AtomicCapabilityIds.DESKTOP_CLICK,
            run_id="run_control",
            expires_at=utc_now() + timedelta(minutes=5),
          ),
          CapabilityGrant(
            grant_id="grant_mobile",
            capability_id=AtomicCapabilityIds.MOBILE_DUMP_UI,
            run_id="run_control",
            expires_at=utc_now() + timedelta(minutes=5),
          ),
        ]
      ),
      local_tools,
      control_workbench=ControlWorkbench(backend),
    )

    desktop_call = provider.normalize_call(
      "desktop_click",
      {"target_id": "window_1", "x": 10, "y": 20},
      run_id="run_control",
      scope="scope",
    )
    mobile_call = provider.normalize_call(
      "mobile_dump_ui",
      {"target_id": "device_1"},
      run_id="run_control",
      scope="scope",
    )
    desktop = await runtime.call(desktop_call.capability_id, desktop_call.input, CapabilityCallContext("run_control"))
    mobile = await runtime.call(mobile_call.capability_id, mobile_call.input, CapabilityCallContext("run_control"))

    self.assertTrue(desktop.result.ok)
    self.assertTrue(mobile.result.ok)
    self.assertEqual(backend.commands[0].target_kind, "desktop")
    self.assertEqual(backend.commands[0].action, "click")
    self.assertEqual(backend.commands[0].payload, {"x": 10, "y": 20})
    self.assertEqual(backend.commands[1].target_kind, "mobile")
    self.assertEqual(backend.commands[1].action, "dump_ui")
    self.assertEqual(mobile.result.output["nodes"][0]["text"], "Pay")

  async def test_model_visible_schema_exposes_default_atomic_surface(self) -> None:
    provider = AtomicCapabilityProvider()

    names = {schema["function"]["name"] for schema in provider.tool_schemas()}

    self.assertIn("http_request", names)
    self.assertIn("desktop_click", names)
    self.assertIn("mobile_dump_ui", names)
    self.assertIn("memory_evolution_note", names)


class _RecordingHTTPClient:
  def __init__(self) -> None:
    self.requests = []

  def request(self, method, url, *, headers=None, body=None, timeout_seconds=None):
    self.requests.append((method, url, headers or {}, body, timeout_seconds))
    return HTTPResponse(status=200, headers={"content-type": "text/plain"}, body="ok", url=url)


if __name__ == "__main__":
  unittest.main()
