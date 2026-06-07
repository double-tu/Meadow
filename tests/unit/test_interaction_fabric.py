import unittest

from agent_kernel.agents import (
  AgentPoolScheduler,
  GroupChatService,
  InteractionFabric,
  ObserverService,
  TaskBoardService,
)
from agent_kernel.domain import AgentPool, ArtifactRef, HandoffRecord, InteractionParticipant, ParticipantKind, PatchArtifact, ReviewRecord, WorkspaceLease
from agent_kernel.persistence import UnitOfWork, connect_sqlite
from agent_kernel.runtime import unit_of_work_factory


class InteractionFabricTests(unittest.TestCase):
  def test_channel_message_round_trip(self) -> None:
    conn = connect_sqlite()
    try:
      fabric = InteractionFabric(unit_of_work_factory(conn))
      participant = fabric.add_participant(
        InteractionParticipant(
          participant_id="participant_1",
          kind=ParticipantKind.AGENT,
          role="backend",
          agent_session_id="session_1",
        )
      )
      channel = fabric.create_channel("API design", [participant.participant_id])

      fabric.send_message(channel.channel_id, participant.participant_id, {"text": "hello"})
      messages = fabric.list_messages(channel.channel_id)

      self.assertEqual(messages[0].content["text"], "hello")
    finally:
      conn.close()

  def test_group_chat_round_robin_turns(self) -> None:
    conn = connect_sqlite()
    try:
      fabric = InteractionFabric(unit_of_work_factory(conn))
      channel = fabric.create_channel("debate", ["p1", "p2"])
      service = GroupChatService(unit_of_work_factory(conn), fabric)
      chat = service.create("thread_1", "topic", ["p1", "p2"])

      turn_1 = service.add_turn(chat, channel.channel_id, {"text": "first"})
      turn_2 = service.add_turn(chat, channel.channel_id, {"text": "second"})

      self.assertEqual(turn_1.speaker_participant_id, "p1")
      self.assertEqual(turn_2.speaker_participant_id, "p2")
    finally:
      conn.close()

  def test_agent_pool_taskboard_and_observer(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      pool = AgentPool(
        pool_id="pool_1",
        name="backend",
        role="backend",
        agent_session_ids=["session_1", "session_2"],
      )
      with UnitOfWork(conn) as uow:
        uow.interactions.save_agent_pool(pool)

      selected = AgentPoolScheduler().select(pool)
      board = TaskBoardService(uow_factory)
      item = board.create_item("implement API", assignee_pool_id=pool.pool_id)
      assigned = board.assign(item, selected)
      finding = ObserverService(uow_factory).request_pause(
        observer_id="observer_1",
        target_run_id="run_1",
        message="dangerous command detected",
      )

      with UnitOfWork(conn) as uow:
        findings = uow.interactions.list_findings("run_1")

      self.assertEqual(selected, "session_1")
      self.assertEqual(assigned.status, "doing")
      self.assertEqual(finding.action, "request_pause")
      self.assertEqual(findings[0].severity, "critical")
    finally:
      conn.close()

  def test_workspace_records_are_serializable(self) -> None:
    lease = WorkspaceLease(
      lease_id="lease_1",
      task_id="task_1",
      agent_session_id="session_1",
      workspace_uri="workspace://lease_1",
    )
    patch = PatchArtifact(
      patch_id="patch_1",
      lease_id="lease_1",
      task_id="task_1",
      author_session_id="session_1",
      artifact_ref=ArtifactRef(artifact_id="artifact_1", uri="artifact://patch"),
      summary="summary",
    )
    review = ReviewRecord(
      review_id="review_1",
      patch_id="patch_1",
      reviewer_id="reviewer_1",
      decision="approved",
    )

    self.assertEqual(lease.to_dict()["status"], "active")
    self.assertEqual(patch.to_dict()["artifact_ref"]["artifact_id"], "artifact_1")
    self.assertEqual(review.to_dict()["decision"], "approved")

  def test_handoff_record_is_serializable(self) -> None:
    handoff = HandoffRecord(
      handoff_id="handoff_1",
      task_id="task_1",
      from_participant_id="p1",
      to_participant_id="p2",
      reason="needs review",
      state_summary="implementation complete",
      expected_output="review result",
      constraints=["preserve API"],
      acceptance_criteria=["tests pass"],
    )

    data = handoff.to_dict()

    self.assertEqual(data["status"], "requested")
    self.assertEqual(data["acceptance_criteria"], ["tests pass"])


if __name__ == "__main__":
  unittest.main()
