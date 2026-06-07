import json
import sys
import unittest

from agent_kernel.app.mcp_config import MCPConfigService
from agent_kernel.persistence import connect_sqlite
from agent_kernel.runtime import unit_of_work_factory


class MCPConfigServiceTests(unittest.TestCase):
  def test_import_filter_export_and_build_stdio_client_commands(self) -> None:
    conn = connect_sqlite()
    try:
      service = MCPConfigService(unit_of_work_factory(conn))

      imported = service.import_config(
        {
          "mcp_servers": {
            "echo": {
              "description": "echo server",
              "enabled": True,
              "agent_types": ["codex"],
              "transport": {
                "type": "stdio",
                "command": sys.executable,
                "args": ["-u", "-c", "print('ready')"],
                "env": {"MCP_TEST": "1"},
                "timeout_seconds": 7,
              },
            },
            "disabled": {
              "enabled": False,
              "transport": {"type": "http", "url": "http://127.0.0.1:19998/mcp"},
            },
          }
        }
      )

      exported = service.export_config(enabled_only=True, agent_type="codex")
      client = service.build_stdio_client(agent_type="codex")
      commands = client._commands  # Intentional white-box check for assembled config.

      self.assertEqual([server.name for server in imported], ["echo", "disabled"])
      self.assertEqual(len(exported["mcp_servers"]), 1)
      self.assertEqual(exported["mcp_servers"][0]["name"], "echo")
      self.assertEqual(commands["echo"].argv[:3], [sys.executable, "-u", "-c"])
      self.assertEqual(commands["echo"].env, {"MCP_TEST": "1"})
      self.assertEqual(commands["echo"].request_timeout_seconds, 7)
    finally:
      conn.close()

  def test_upsert_delete_and_json_roundtrip(self) -> None:
    conn = connect_sqlite()
    try:
      service = MCPConfigService(unit_of_work_factory(conn))
      server = service.upsert_server(
        {
          "name": "browser-tools",
          "transport": {"type": "stdio", "command": "node", "args": ["server.js"]},
        }
      )
      payload = json.loads(json.dumps(server.to_dict()))

      self.assertEqual(service.get_server("browser-tools").transport.command, "node")
      self.assertEqual(payload["transport"]["args"], ["server.js"])
      self.assertTrue(service.delete_server("browser-tools"))
      self.assertIsNone(service.get_server("browser-tools"))
    finally:
      conn.close()


if __name__ == "__main__":
  unittest.main()
