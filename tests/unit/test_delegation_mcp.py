import json
from pathlib import Path
import sys
import tempfile
import unittest

from agent_kernel.agents import AgentDelegationBroker, DelegationMCPServer, FakeAgentConnector
from agent_kernel.capabilities.adapters import MCPServerCommand, StdioMCPClient
from agent_kernel.domain import DelegationStatus
from agent_kernel.persistence import connect_sqlite
from agent_kernel.runtime import unit_of_work_factory


class DelegationMCPServerTests(unittest.IsolatedAsyncioTestCase):
  async def test_delegation_mcp_server_lists_and_calls_tools(self) -> None:
    conn = connect_sqlite()
    try:
      connector = FakeAgentConnector()
      broker = AgentDelegationBroker(unit_of_work_factory(conn), {"codex": connector})
      server = DelegationMCPServer(broker, default_parent_run_id="run_mcp")

      initialized = await server.handle_frame(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
      )
      listed = await server.handle_frame({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
      delegated = await server.handle_frame(
        {
          "jsonrpc": "2.0",
          "id": 3,
          "method": "tools/call",
          "params": {
            "name": "delegate_to_agent",
            "arguments": {
              "agent_type": "codex",
              "task": "inspect code",
              "metadata": {"purpose": "test"},
            },
          },
        }
      )
      task_id = delegated["result"]["structuredContent"]["task"]["task_id"]
      await broker.await_task(task_id)
      status = await server.handle_frame(
        {
          "jsonrpc": "2.0",
          "id": 4,
          "method": "tools/call",
          "params": {
            "name": "get_delegation_status",
            "arguments": {"task_ids": [task_id]},
          },
        }
      )

      self.assertEqual(initialized["result"]["capabilities"], {"tools": {}})
      self.assertEqual(listed["result"]["tools"][0]["name"], "delegate_to_agent")
      self.assertEqual(delegated["result"]["structuredContent"]["task"]["status"], "running")
      self.assertEqual(status["result"]["structuredContent"]["tasks"][0]["status"], DelegationStatus.COMPLETED)
      self.assertEqual(connector.messages[0].content["metadata"]["purpose"], "test")
    finally:
      conn.close()

  async def test_stdio_mcp_client_runs_cli_delegation_sidecar(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      db = str(Path(tmp) / "kernel.sqlite")
      config_path = Path(tmp) / "agent-kernel.json"
      config_path.write_text(
        json.dumps(
          {
            "agent_connectors": [
              {
                "connector_id": "worker",
                "product": "worker",
                "argv": _jsonl_worker_argv(),
                "startup_timeout_seconds": 2,
                "turn_timeout_seconds": 2,
              }
            ]
          }
        ),
        encoding="utf-8",
      )
      client = StdioMCPClient(
        {
          "delegation": MCPServerCommand(
            argv=[
              sys.executable,
              "-u",
              "-m",
              "agent_kernel.hosts.cli",
              "--db",
              db,
              "--config",
              str(config_path),
              "mcp-delegation-server",
              "--parent-run-id",
              "run_sidecar",
            ],
            cwd=str(Path(__file__).resolve().parents[2]),
            startup_timeout_seconds=3,
            request_timeout_seconds=3,
          )
        }
      )
      try:
        tools = await client.list_tools("delegation")
        delegated = await client.call_tool(
          "delegation",
          "delegate_to_agent",
          {"agent_type": "worker", "task": "summarize"},
        )
        task_id = delegated.output["structuredContent"]["task"]["task_id"]
        status = await client.call_tool(
          "delegation",
          "get_delegation_status",
          {"task_ids": [task_id], "wait_ms": 500},
        )
      finally:
        await client.stop_all()

    self.assertEqual(tools[0]["name"], "delegate_to_agent")
    self.assertTrue(delegated.ok)
    self.assertEqual(status.output["structuredContent"]["tasks"][0]["status"], "completed")
    self.assertEqual(status.output["structuredContent"]["tasks"][0]["output"]["task"], "summarize")


def _jsonl_worker_argv() -> list[str]:
  return [
    sys.executable,
    "-u",
    "-c",
    (
      "import json, sys\n"
      "for line in sys.stdin:\n"
      "    frame = json.loads(line)\n"
      "    if frame['type'] == 'start':\n"
      "        print(json.dumps({'type': 'started', 'session_id': frame['session_id']}), flush=True)\n"
      "    elif frame['type'] == 'message':\n"
      "        task = frame['content']['task']\n"
      "        print(json.dumps({'type': 'turn', 'turn_id': 'turn_worker', "
      "'output': {'task': task}, 'completed': True}), flush=True)\n"
      "    elif frame['type'] == 'stop':\n"
      "        break\n"
    ),
  ]


if __name__ == "__main__":
  unittest.main()
