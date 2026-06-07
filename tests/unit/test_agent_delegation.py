import asyncio
import unittest

from agent_kernel.agents import (
  AgentDelegationBroker,
  AgentDelegationBrokerOptions,
  ConnectorMessage,
  ConnectorTurn,
  FakeAgentConnector,
)
from agent_kernel.domain import DelegationStatus, RuntimeEventType
from agent_kernel.domain.delegation import DelegationTask
from agent_kernel.persistence import UnitOfWork, connect_sqlite
from agent_kernel.runtime import unit_of_work_factory


class AgentDelegationBrokerTests(unittest.IsolatedAsyncioTestCase):
  async def test_delegate_completes_through_connector_and_persists_events(self) -> None:
    conn = connect_sqlite()
    try:
      connector = FakeAgentConnector()
      connector.queue_response(
        ConnectorTurn(
          turn_id="turn_delegate",
          session_id="unused",
          output={"summary": "implemented"},
          completed=True,
        )
      )
      broker = AgentDelegationBroker(unit_of_work_factory(conn), {"codex": connector})

      started = await broker.delegate(
        parent_run_id="run_parent",
        parent_agent_id="agent_parent",
        connector_id="codex",
        agent_type="implementation",
        task="implement endpoint",
      )
      await broker.await_task(started.task_id)
      reports = await broker.get_status(parent_run_id="run_parent", task_ids=[started.task_id])

      self.assertEqual(started.status, DelegationStatus.RUNNING)
      self.assertEqual(reports[0].status, DelegationStatus.COMPLETED)
      self.assertEqual(reports[0].output, {"summary": "implemented"})
      self.assertEqual(connector.started, [started.connector_session_id])
      self.assertEqual(connector.messages[0].content["task"], "implement endpoint")
      with UnitOfWork(conn) as uow:
        events = uow.events.list_by_run("run_parent")
      event_types = [event.event_type for event in events]
      self.assertIn(RuntimeEventType.AGENT_DELEGATION_STARTED, event_types)
      self.assertIn(RuntimeEventType.AGENT_DELEGATION_COMPLETED, event_types)
    finally:
      conn.close()

  async def test_parent_scoping_hides_other_parent_tasks(self) -> None:
    conn = connect_sqlite()
    try:
      connector = FakeAgentConnector()
      broker = AgentDelegationBroker(unit_of_work_factory(conn), {"worker": connector})

      report = await broker.delegate(
        parent_run_id="run_owner",
        connector_id="worker",
        task="private task",
      )
      visible = await broker.get_status(parent_run_id="run_owner", task_ids=[report.task_id])
      hidden = await broker.get_status(parent_run_id="run_other", task_ids=[report.task_id])

      self.assertEqual(visible[0].status, DelegationStatus.RUNNING)
      self.assertEqual(hidden[0].status, DelegationStatus.UNKNOWN)
      await broker.cancel(parent_run_id="run_owner", task_id=report.task_id)
    finally:
      conn.close()

  async def test_get_status_waits_until_task_finishes(self) -> None:
    conn = connect_sqlite()
    try:
      connector = _DelayedConnector(delay_seconds=0.05)
      broker = AgentDelegationBroker(unit_of_work_factory(conn), {"worker": connector})

      report = await broker.delegate(
        parent_run_id="run_wait",
        connector_id="worker",
        task="waited task",
      )
      waited = await broker.get_status(parent_run_id="run_wait", task_ids=[report.task_id], wait_ms=500)

      self.assertEqual(waited[0].status, DelegationStatus.COMPLETED)
      self.assertEqual(waited[0].output, {"done": "waited task"})
    finally:
      conn.close()

  async def test_cancel_running_delegation_stops_connector_and_persists_terminal_status(self) -> None:
    conn = connect_sqlite()
    try:
      connector = _BlockedConnector()
      broker = AgentDelegationBroker(unit_of_work_factory(conn), {"worker": connector})

      report = await broker.delegate(
        parent_run_id="run_cancel",
        connector_id="worker",
        task="long task",
      )
      await asyncio.sleep(0)
      cancelled = await broker.cancel(
        parent_run_id="run_cancel",
        task_id=report.task_id,
        reason="not needed",
      )
      status = await broker.get_status(parent_run_id="run_cancel", task_ids=[report.task_id])

      self.assertEqual(cancelled.status, DelegationStatus.CANCELLED)
      self.assertEqual(status[0].status, DelegationStatus.CANCELLED)
      self.assertEqual(connector.stopped, [(report.connector_session_id, "not needed")])
      with UnitOfWork(conn) as uow:
        events = uow.events.list_by_run("run_cancel")
      self.assertIn(RuntimeEventType.AGENT_DELEGATION_CANCELLED, [event.event_type for event in events])
    finally:
      conn.close()

  async def test_depth_limit_rejects_recursive_delegation(self) -> None:
    conn = connect_sqlite()
    try:
      connector = FakeAgentConnector()
      broker = AgentDelegationBroker(
        unit_of_work_factory(conn),
        {"worker": connector},
        options=AgentDelegationBrokerOptions(depth_limit=1),
      )

      with self.assertRaisesRegex(ValueError, "depth limit exceeded"):
        await broker.delegate(
          parent_run_id="run_depth",
          connector_id="worker",
          task="spawn too deep",
          metadata={"delegation_depth": 1},
        )
    finally:
      conn.close()

  async def test_cancel_parent_cascades_running_delegations(self) -> None:
    conn = connect_sqlite()
    try:
      connector = _BlockedConnector()
      broker = AgentDelegationBroker(unit_of_work_factory(conn), {"worker": connector})

      first = await broker.delegate(parent_run_id="run_parent_cancel", connector_id="worker", task="first")
      second = await broker.delegate(parent_run_id="run_parent_cancel", connector_id="worker", task="second")
      other = await broker.delegate(parent_run_id="other_parent", connector_id="worker", task="other")
      await asyncio.sleep(0)

      cancelled = await broker.cancel_parent(parent_run_id="run_parent_cancel", reason="parent stopped")
      visible = await broker.get_status(parent_run_id="run_parent_cancel")
      other_visible = await broker.get_status(parent_run_id="other_parent", task_ids=[other.task_id])

      self.assertEqual([report.status for report in cancelled], [DelegationStatus.CANCELLED, DelegationStatus.CANCELLED])
      self.assertEqual([report.status for report in visible], [DelegationStatus.CANCELLED, DelegationStatus.CANCELLED])
      self.assertEqual(other_visible[0].status, DelegationStatus.RUNNING)
      self.assertEqual(
        connector.stopped,
        [
          (first.connector_session_id, "parent stopped"),
          (second.connector_session_id, "parent stopped"),
        ],
      )
      await broker.cancel(parent_run_id="other_parent", task_id=other.task_id)
    finally:
      conn.close()

  async def test_recover_orphaned_running_marks_unattached_tasks_failed(self) -> None:
    conn = connect_sqlite()
    try:
      with UnitOfWork(conn) as uow:
        uow.interactions.save_delegation_task(
          DelegationTask(
            task_id="delegation_orphaned",
            parent_run_id="run_recover",
            connector_id="worker",
            connector_session_id="connector_session_orphaned",
            task="stale task",
          )
        )
      broker = AgentDelegationBroker(unit_of_work_factory(conn), {"worker": FakeAgentConnector()})

      recovered = broker.recover_orphaned_running(reason="host restarted")
      status = await broker.get_status(parent_run_id="run_recover", task_ids=["delegation_orphaned"])

      self.assertEqual(recovered[0].status, DelegationStatus.FAILED)
      self.assertEqual(recovered[0].error["type"], "orphaned_delegation")
      self.assertEqual(status[0].status, DelegationStatus.FAILED)
      with UnitOfWork(conn) as uow:
        events = uow.events.list_by_run("run_recover")
      self.assertEqual(events[-1].event_type, RuntimeEventType.AGENT_DELEGATION_FAILED)
      self.assertEqual(events[-1].payload["recovery"], "orphaned_running_failed")
    finally:
      conn.close()

  async def test_large_delegation_result_is_handed_off_as_artifact(self) -> None:
    conn = connect_sqlite()
    try:
      connector = FakeAgentConnector()
      connector.queue_response(
        ConnectorTurn(
          turn_id="turn_large",
          session_id="unused",
          output={"text": "x" * 200},
          completed=True,
        )
      )
      broker = AgentDelegationBroker(
        unit_of_work_factory(conn),
        {"worker": connector},
        options=AgentDelegationBrokerOptions(result_artifact_threshold_chars=20),
      )

      started = await broker.delegate(
        parent_run_id="run_large_result",
        connector_id="worker",
        task="large result",
      )
      await broker.await_task(started.task_id)
      reports = await broker.get_status(parent_run_id="run_large_result", task_ids=[started.task_id])

      artifact_id = reports[0].result_artifact_refs[0].artifact_id
      with UnitOfWork(conn) as uow:
        artifact = uow.artifacts.get(artifact_id)
        metadata = uow.artifacts.get_metadata(artifact_id)
        events = uow.events.list_by_run("run_large_result")

      self.assertTrue(reports[0].output["truncated"])
      self.assertEqual(artifact.uri, f"artifact://delegations/{started.task_id}/result")
      self.assertIn('"text"', metadata["output_json"])
      self.assertEqual(events[-1].artifact_refs[0].artifact_id, artifact_id)
    finally:
      conn.close()


class _DelayedConnector:
  def __init__(self, delay_seconds: float) -> None:
    self.delay_seconds = delay_seconds
    self.started: list[str] = []
    self.stopped: list[tuple[str, str]] = []

  async def start(self, session_id: str, metadata=None) -> None:
    self.started.append(session_id)

  async def send(self, message: ConnectorMessage) -> ConnectorTurn:
    await asyncio.sleep(self.delay_seconds)
    return ConnectorTurn(
      turn_id="turn_delayed",
      session_id=message.session_id,
      output={"done": message.content["task"]},
      completed=True,
    )

  async def stop(self, session_id: str, reason: str) -> None:
    self.stopped.append((session_id, reason))


class _BlockedConnector:
  def __init__(self) -> None:
    self.started: list[str] = []
    self.stopped: list[tuple[str, str]] = []
    self._event = asyncio.Event()

  async def start(self, session_id: str, metadata=None) -> None:
    self.started.append(session_id)

  async def send(self, message: ConnectorMessage) -> ConnectorTurn:
    await self._event.wait()
    return ConnectorTurn(
      turn_id="turn_blocked",
      session_id=message.session_id,
      output={"unexpected": True},
      completed=True,
    )

  async def stop(self, session_id: str, reason: str) -> None:
    self.stopped.append((session_id, reason))


if __name__ == "__main__":
  unittest.main()
