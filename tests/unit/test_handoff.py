import unittest

from agent_kernel.agents import HandoffService, InteractionFabric
from agent_kernel.domain import ArtifactRef, ChannelMode, InteractionParticipant, ParticipantKind
from agent_kernel.persistence import UnitOfWork, connect_sqlite
from agent_kernel.runtime import unit_of_work_factory


class HandoffTests(unittest.TestCase):
  def test_handoff_persists_lineage_and_posts_target_channel_message(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      fabric = InteractionFabric(uow_factory)
      fabric.add_participant(
        InteractionParticipant("p_backend", ParticipantKind.AGENT, "backend")
      )
      fabric.add_participant(
        InteractionParticipant("p_tests", ParticipantKind.AGENT, "tests")
      )
      channel = fabric.create_channel(
        "implementation handoff",
        ["p_backend", "p_tests"],
        mode=ChannelMode.REQUEST_RESPONSE,
      )

      handoff = HandoffService(uow_factory).request(
        task_id="task_1",
        from_participant_id="p_backend",
        to_participant_id="p_tests",
        reason="implementation is ready for validation",
        state_summary="API route implemented; tests not yet run.",
        expected_output="test report and review notes",
        target_channel_id=channel.channel_id,
        constraints=["do not modify production config"],
        acceptance_criteria=["all unit tests pass"],
        artifact_refs=[ArtifactRef("artifact_patch", "artifact://patch")],
      )
      accepted = HandoffService(uow_factory).accept(handoff.handoff_id)

      with UnitOfWork(conn) as uow:
        handoffs = uow.interactions.list_handoffs("task_1")
        messages = uow.interactions.list_messages(channel.channel_id)

      self.assertEqual(accepted.status, "accepted")
      self.assertEqual(handoffs[0].from_participant_id, "p_backend")
      self.assertEqual(handoffs[0].to_participant_id, "p_tests")
      self.assertEqual(handoffs[0].state_summary, "API route implemented; tests not yet run.")
      self.assertEqual(messages[0].content["type"], "handoff")
      self.assertEqual(messages[0].content["handoff_id"], handoff.handoff_id)
      self.assertEqual(messages[0].artifact_refs[0].artifact_id, "artifact_patch")
    finally:
      conn.close()

  def test_handoff_policy_rejects_target_not_in_channel(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      fabric = InteractionFabric(uow_factory)
      channel = fabric.create_channel("handoff", ["p_backend"])

      with self.assertRaises(ValueError):
        HandoffService(uow_factory).request(
          task_id="task_1",
          from_participant_id="p_backend",
          to_participant_id="p_tests",
          reason="needs tests",
          state_summary="ready",
          expected_output="test report",
          target_channel_id=channel.channel_id,
        )
    finally:
      conn.close()


if __name__ == "__main__":
  unittest.main()
