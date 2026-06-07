import unittest

from agent_kernel.agents import ConnectorMessage, ConnectorTurn, FakeAgentConnector
from agent_kernel.hosts.dto import EventStreamEnvelope, TaskWorkspaceDTO, default_http_routes


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


if __name__ == "__main__":
  unittest.main()
