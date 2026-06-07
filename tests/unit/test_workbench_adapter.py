import json
import unittest
from unittest import mock

from agent_kernel.capabilities.adapters import (
  HTTPWorkbenchClient,
  HTTPWorkbenchEndpoint,
  WorkbenchCommand,
)


class HTTPWorkbenchClientTests(unittest.IsolatedAsyncioTestCase):
  async def test_http_workbench_client_posts_command_and_reads_envelope(self) -> None:
    captured = {}

    def fake_urlopen(request, timeout):
      captured["url"] = request.full_url
      captured["timeout"] = timeout
      captured["headers"] = dict(request.header_items())
      captured["body"] = json.loads(request.data.decode("utf-8"))
      return _FakeHTTPResponse({"ok": True, "output": {"status": "ok"}})

    client = HTTPWorkbenchClient(
      HTTPWorkbenchEndpoint(
        base_url="http://workbench.local/api",
        path="/commands",
        timeout_seconds=2,
        headers={"Authorization": "Bearer token"},
      )
    )

    with mock.patch("agent_kernel.capabilities.adapters.workbench.url_request.urlopen", fake_urlopen):
      result = await client.execute(
        WorkbenchCommand(
          command_id="cmd_1",
          kind="inspect",
          payload={"target": "workspace"},
        )
      )

    self.assertTrue(result.ok)
    self.assertEqual(result.output, {"status": "ok"})
    self.assertEqual(captured["url"], "http://workbench.local/api/commands")
    self.assertEqual(captured["timeout"], 2)
    self.assertEqual(captured["body"]["kind"], "inspect")
    self.assertEqual(captured["body"]["payload"], {"target": "workspace"})
    self.assertEqual(captured["headers"]["Authorization"], "Bearer token")

  async def test_http_workbench_client_wraps_plain_json_response(self) -> None:
    def fake_urlopen(request, timeout):
      return _FakeHTTPResponse({"value": 3})

    client = HTTPWorkbenchClient(HTTPWorkbenchEndpoint(base_url="http://workbench.local"))

    with mock.patch("agent_kernel.capabilities.adapters.workbench.url_request.urlopen", fake_urlopen):
      result = await client.execute(WorkbenchCommand(command_id="cmd_2", kind="measure"))

    self.assertTrue(result.ok)
    self.assertEqual(result.output, {"value": 3})


class _FakeHTTPResponse:
  status = 200

  def __init__(self, payload: dict[str, object]) -> None:
    self._payload = payload

  def __enter__(self):
    return self

  def __exit__(self, exc_type, exc, traceback) -> None:
    return None

  def read(self) -> bytes:
    return json.dumps(self._payload).encode("utf-8")


if __name__ == "__main__":
  unittest.main()
