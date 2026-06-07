import unittest
import json
import sys
import tempfile
from pathlib import Path

from agent_kernel.agents import (
  AgentConnectorRouter,
  ConnectorMessage,
  ConnectorRoute,
  ConnectorTurn,
  FakeAgentConnector,
  InteractionFabric,
  ProductCLIConnectorFactory,
  ProductCLIConnectorSpec,
  ProductCLIShimProfile,
  StdioAgentCommand,
  StructuredStdioAgentConnector,
  load_product_cli_connector_specs,
  product_cli_connector_spec_from_config,
)
from agent_kernel.hosts.dto import EventStreamEnvelope, TaskWorkspaceDTO, default_http_routes
from agent_kernel.persistence import connect_sqlite
from agent_kernel.runtime import unit_of_work_factory


class ConnectorsAndHostDTOTests(unittest.IsolatedAsyncioTestCase):
  async def test_fake_agent_connector_round_trip(self) -> None:
    connector = FakeAgentConnector()
    connector.queue_response(
      ConnectorTurn(
        turn_id="turn_1",
        session_id="session_1",
        output={"summary": "done"},
        completed=True,
      )
    )

    await connector.start("session_1")
    turn = await connector.send(
      ConnectorMessage(message_id="msg_1", session_id="session_1", content={"task": "do"})
    )
    await connector.stop("session_1", "finished")

    self.assertEqual(connector.started, ["session_1"])
    self.assertTrue(turn.completed)
    self.assertEqual(turn.output["summary"], "done")
    self.assertEqual(connector.stopped, [("session_1", "finished")])

  async def test_agent_connector_router_routes_multiple_external_sessions_to_channel(self) -> None:
    conn = connect_sqlite()
    try:
      fabric = InteractionFabric(unit_of_work_factory(conn))
      channel = fabric.create_channel("implementation", ["p_codex", "p_review"])
      codex = FakeAgentConnector()
      reviewer = FakeAgentConnector()
      codex.queue_response(
        ConnectorTurn(
          turn_id="turn_codex",
          session_id="session_codex",
          output={"patch": "implemented"},
          completed=False,
        )
      )
      reviewer.queue_response(
        ConnectorTurn(
          turn_id="turn_review",
          session_id="session_review",
          output={"review": "approved"},
          completed=True,
        )
      )
      router = AgentConnectorRouter({"codex": codex, "reviewer": reviewer}, fabric=fabric)
      router.bind(
        ConnectorRoute(
          participant_id="p_codex",
          session_id="session_codex",
          connector_id="codex",
          metadata={"model": "coding-agent"},
        )
      )
      router.bind(
        ConnectorRoute(
          participant_id="p_review",
          session_id="session_review",
          connector_id="reviewer",
        )
      )

      await router.start_all()
      codex_turn = await router.send_to_participant(
        "p_codex",
        {"task": "implement API"},
        channel_id=channel.channel_id,
      )
      review_turn = await router.send_to_participant(
        "p_review",
        {"task": "review patch"},
        channel_id=channel.channel_id,
      )
      await router.stop_all("task complete")
      messages = fabric.list_messages(channel.channel_id)

      self.assertEqual(codex.started, ["session_codex"])
      self.assertEqual(reviewer.started, ["session_review"])
      self.assertEqual(codex.messages[0].content["task"], "implement API")
      self.assertEqual(reviewer.messages[0].content["task"], "review patch")
      self.assertEqual(codex_turn.turn.output["patch"], "implemented")
      self.assertTrue(review_turn.turn.completed)
      self.assertEqual(messages[0].content["type"], "connector_turn")
      self.assertEqual(messages[0].content["connector_id"], "codex")
      self.assertEqual(messages[1].content["output"], {"review": "approved"})
      self.assertEqual(codex.stopped, [("session_codex", "task complete")])
      self.assertEqual(reviewer.stopped, [("session_review", "task complete")])
    finally:
      conn.close()

  async def test_structured_stdio_agent_connector_uses_jsonl_protocol(self) -> None:
    connector = StructuredStdioAgentConnector(
      StdioAgentCommand(
        argv=[
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
            "        print(json.dumps({'type': 'turn', 'turn_id': 'turn_' + frame['message_id'], "
            "'output': {'received': frame['content']}, 'completed': True}), flush=True)\n"
            "    elif frame['type'] == 'stop':\n"
            "        break\n"
          ),
        ],
        startup_timeout_seconds=2,
        turn_timeout_seconds=2,
      )
    )

    await connector.start("session_stdio", metadata={"agent": "shim"})
    turn = await connector.send(
      ConnectorMessage(
        message_id="msg_1",
        session_id="session_stdio",
        content={"task": "structured"},
      )
    )
    await connector.stop("session_stdio", "done")

    self.assertEqual(turn.turn_id, "turn_msg_1")
    self.assertTrue(turn.completed)
    self.assertEqual(turn.output["received"], {"task": "structured"})

  async def test_product_cli_connector_factory_routes_multiple_jsonl_shims(self) -> None:
    conn = connect_sqlite()
    try:
      fabric = InteractionFabric(unit_of_work_factory(conn))
      channel = fabric.create_channel("product routing", ["p_impl", "p_review"])
      specs = [
        ProductCLIConnectorSpec.from_dict(
          {
            "connector_id": "impl_cli",
            "product": "implementation-cli",
            "argv": _jsonl_shim_argv("implementation-cli"),
            "startup_timeout_seconds": 2,
            "turn_timeout_seconds": 2,
          }
        ),
        ProductCLIConnectorSpec.from_dict(
          {
            "connector_id": "review_cli",
            "product": "review-cli",
            "argv": _jsonl_shim_argv("review-cli"),
            "startup_timeout_seconds": 2,
            "turn_timeout_seconds": 2,
          }
        ),
      ]
      connectors = ProductCLIConnectorFactory().build_many(specs)
      router = AgentConnectorRouter(connectors, fabric=fabric)
      router.bind(ConnectorRoute("p_impl", "session_impl", "impl_cli", {"role": "builder"}))
      router.bind(ConnectorRoute("p_review", "session_review", "review_cli", {"role": "reviewer"}))

      await router.start_all()
      impl_turn = await router.send_to_participant(
        "p_impl",
        {"task": "implement"},
        channel_id=channel.channel_id,
      )
      review_turn = await router.send_to_participant(
        "p_review",
        {"task": "review"},
        channel_id=channel.channel_id,
      )
      await router.stop_all("done")
      messages = fabric.list_messages(channel.channel_id)

      self.assertEqual(impl_turn.turn.output["product"], "implementation-cli")
      self.assertEqual(review_turn.turn.output["product"], "review-cli")
      self.assertTrue(impl_turn.turn.completed)
      self.assertTrue(review_turn.turn.completed)
      self.assertEqual(messages[0].content["connector_id"], "impl_cli")
      self.assertEqual(messages[1].content["connector_id"], "review_cli")
    finally:
      conn.close()

  async def test_product_cli_shim_profile_runs_real_subprocess_cli(self) -> None:
    profile = ProductCLIShimProfile.codex(
      executable=sys.executable,
      default_args=[
        "-c",
        (
          "import sys\n"
          "prompt = sys.stdin.read()\n"
          "print('codex-shim:' + prompt.strip())\n"
        ),
      ],
      request_timeout_seconds=2,
    )
    spec = profile.to_connector_spec(
      "codex_cli",
      startup_timeout_seconds=2,
      turn_timeout_seconds=4,
      metadata={"role": "implementation"},
    )
    connector = ProductCLIConnectorFactory().build(spec)

    await connector.start("session_codex", metadata={"agent": "codex"})
    turn = await connector.send(
      ConnectorMessage(
        message_id="msg_codex",
        session_id="session_codex",
        content={"task": "implement feature"},
      )
    )
    await connector.stop("session_codex", "done")

    self.assertTrue(turn.completed)
    self.assertEqual(turn.output["product"], "codex")
    self.assertEqual(turn.output["stdout"], "codex-shim:implement feature\n")
    self.assertEqual(spec.metadata["shim"], "agent_kernel.agents.cli_shim")

  def test_product_cli_shim_profiles_generate_neutral_specs(self) -> None:
    profiles = [
      ProductCLIShimProfile.codex(executable="codex", default_args=["--model", "fast"]),
      ProductCLIShimProfile.claude(executable="claude", prompt_mode="argument", prompt_argument="-p"),
      ProductCLIShimProfile.gemini(executable="gemini", prompt_mode="json_stdin", output_format="json"),
    ]

    specs = [
      profile.to_connector_spec(f"{profile.product}_cli", startup_timeout_seconds=1)
      for profile in profiles
    ]

    self.assertEqual([spec.product for spec in specs], ["codex", "claude", "gemini"])
    self.assertTrue(all("agent_kernel.agents.cli_shim" in spec.argv for spec in specs))
    self.assertIn("--prompt-argument", specs[1].argv)
    self.assertIn("json_stdin", specs[2].argv)

  def test_product_cli_connector_spec_from_profile_config(self) -> None:
    spec = product_cli_connector_spec_from_config(
      {
        "connector_id": "codex_cli",
        "product": "codex",
        "executable": sys.executable,
        "default_args": ["-c", "print('ok')"],
        "request_timeout_seconds": 2,
        "metadata": {"role": "implementation"},
      }
    )

    self.assertEqual(spec.connector_id, "codex_cli")
    self.assertEqual(spec.product, "codex")
    self.assertIn("agent_kernel.agents.cli_shim", spec.argv)
    self.assertEqual(spec.metadata["role"], "implementation")

  def test_load_product_cli_connector_specs_supports_json_map_and_direct_argv(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      config_path = Path(tmp) / "agent-kernel.json"
      config_path.write_text(
        (
          '{'
          '"agent_connectors": {'
          '"codex_cli": {'
          '"product": "codex", '
          f'"executable": {json.dumps(sys.executable)}, '
          '"default_args": ["-c", "print(\\"ok\\")"], '
          '"request_timeout_seconds": 2'
          '}, '
          '"raw_cli": {'
          '"product": "raw", '
          f'"argv": {json.dumps(_jsonl_shim_argv("raw"))}, '
          '"startup_timeout_seconds": 2, '
          '"turn_timeout_seconds": 2'
          '}'
          '}'
          '}'
        ),
        encoding="utf-8",
      )

      specs = load_product_cli_connector_specs(config_path)

    self.assertEqual([spec.connector_id for spec in specs], ["codex_cli", "raw_cli"])
    self.assertEqual(specs[0].product, "codex")
    self.assertEqual(specs[1].argv, _jsonl_shim_argv("raw"))

  async def test_host_dtos_are_serializable(self) -> None:
    envelope = EventStreamEnvelope(
      event_id="evt_1",
      event_type="run.created",
      run_id="run_1",
      payload={"ok": True},
    )
    workspace = TaskWorkspaceDTO(
      workspace_id="workspace_1",
      title="Project",
      run_ids=["run_1"],
      controls={"cancel": True},
    )
    routes = default_http_routes()

    self.assertEqual(envelope.to_dict()["event_type"], "run.created")
    self.assertEqual(workspace.to_dict()["run_ids"], ["run_1"])
    self.assertIn("/runs/{run_id}/events", [route.path for route in routes])

def _jsonl_shim_argv(product: str) -> list[str]:
  return [
    sys.executable,
    "-u",
    "-c",
    (
      "import json, sys\n"
      f"product = {product!r}\n"
      "for line in sys.stdin:\n"
      "    frame = json.loads(line)\n"
      "    if frame['type'] == 'start':\n"
      "        print(json.dumps({'type': 'started', 'session_id': frame['session_id']}), flush=True)\n"
      "    elif frame['type'] == 'message':\n"
      "        print(json.dumps({'type': 'turn', 'turn_id': product + '_' + frame['message_id'], "
      "'output': {'product': product, 'received': frame['content']}, 'completed': True}), flush=True)\n"
      "    elif frame['type'] == 'stop':\n"
      "        break\n"
    ),
  ]


if __name__ == "__main__":
  unittest.main()
