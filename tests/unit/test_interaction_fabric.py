import asyncio
import unittest

from agent_kernel.agents import (
  AgentPoolScheduler,
  DecisionArtifactService,
  GroupChatService,
  InteractionFabric,
  ObserverService,
  TaskBoardService,
)
from agent_kernel.domain import (
  AgentPool,
  ArtifactRef,
  HandoffRecord,
  InteractionParticipant,
  NodeResult,
  NodeSpec,
  ParticipantKind,
  PatchArtifact,
  ReviewRecord,
  RunStatus,
  RuntimeEventType,
  SpeakerPolicy,
  SpeakerPolicyType,
  WorkspaceLease,
  WorkflowSpec,
)
from agent_kernel.persistence import UnitOfWork, connect_sqlite
from agent_kernel.runtime import RuntimeEngine, unit_of_work_factory
from agent_kernel.workflow import FunctionNodeExecutor, NodeExecutorRegistry


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

  def test_group_chat_free_for_all_accepts_requested_speaker(self) -> None:
    conn = connect_sqlite()
    try:
      fabric = InteractionFabric(unit_of_work_factory(conn))
      channel = fabric.create_channel("free discussion", ["p1", "p2"])
      service = GroupChatService(unit_of_work_factory(conn), fabric)
      chat = service.create(
        "thread_1",
        "topic",
        ["p1", "p2"],
        speaker_policy=SpeakerPolicy(type=SpeakerPolicyType.FREE_FOR_ALL),
      )

      turn = service.add_turn(
        chat,
        channel.channel_id,
        {"speaker_participant_id": "p2", "text": "I can take this."},
      )

      self.assertEqual(turn.speaker_participant_id, "p2")
      self.assertEqual(turn.selected_by, "free_for_all")
      self.assertEqual(turn.rationale, "Speaker provided by caller.")
    finally:
      conn.close()

  def test_group_chat_moderator_select_records_selection_rationale(self) -> None:
    conn = connect_sqlite()
    try:
      fabric = InteractionFabric(unit_of_work_factory(conn))
      channel = fabric.create_channel("moderated discussion", ["moderator", "p1", "p2"])
      service = GroupChatService(unit_of_work_factory(conn), fabric)
      chat = service.create(
        "thread_1",
        "topic",
        ["moderator", "p1", "p2"],
        speaker_policy=SpeakerPolicy(type=SpeakerPolicyType.MODERATOR_SELECT),
        moderator_participant_id="moderator",
      )

      turn = service.add_turn(
        chat,
        channel.channel_id,
        {
          "selected_speaker_participant_id": "p2",
          "selection_rationale": "reviewer has the context",
          "text": "please review",
        },
      )

      self.assertEqual(turn.speaker_participant_id, "p2")
      self.assertEqual(turn.selected_by, "moderator")
      self.assertEqual(turn.rationale, "reviewer has the context")
    finally:
      conn.close()

  def test_group_chat_decision_artifact_summarizes_channel_and_completes_session(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      fabric = InteractionFabric(uow_factory)
      channel = fabric.create_channel("architecture decision", ["moderator", "builder", "reviewer"])
      service = GroupChatService(uow_factory, fabric)
      chat = service.create("thread_1", "replace GenericAgent", ["builder", "reviewer"])
      service.add_turn(chat, channel.channel_id, {"text": "implement control workbench"})
      service.add_turn(chat, channel.channel_id, {"type": "decision", "summary": "ship adapter boundary"})

      artifact = DecisionArtifactService(uow_factory, fabric).create_for_group_chat(
        chat,
        channel.channel_id,
        decided_by_participant_id="moderator",
      )

      with UnitOfWork(conn) as uow:
        metadata = uow.artifacts.get_metadata(artifact.artifact_id)
        saved_chat = uow.interactions.get_group_chat(chat.group_chat_id)
      messages = fabric.list_messages(channel.channel_id)

      self.assertEqual(artifact.media_type, "application/vnd.meadow.decision+json")
      self.assertEqual(metadata["kind"], "decision_artifact")
      self.assertEqual(metadata["payload"]["topic"], "replace GenericAgent")
      self.assertEqual(metadata["payload"]["message_count"], 2)
      self.assertEqual(metadata["payload"]["decision"]["summary"], "ship adapter boundary")
      self.assertEqual(saved_chat.status, "completed")
      self.assertEqual(saved_chat.decision_artifact_ref.artifact_id, artifact.artifact_id)
      self.assertEqual(messages[-1].content["type"], "decision_artifact")
      self.assertEqual(messages[-1].content["artifact_id"], artifact.artifact_id)
    finally:
      conn.close()

  def test_cross_channel_decision_artifact_aggregates_multiple_channels(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      fabric = InteractionFabric(uow_factory)
      implementation = fabric.create_channel("implementation", ["builder", "reviewer"])
      operations = fabric.create_channel("operations", ["operator"])
      fabric.send_message(implementation.channel_id, "builder", {"text": "control adapter ready"})
      fabric.send_message(implementation.channel_id, "reviewer", {"type": "decision", "summary": "approve adapter"})
      fabric.send_message(operations.channel_id, "operator", {"text": "deployment needs env flags"})

      artifact = DecisionArtifactService(uow_factory, fabric).create_cross_channel(
        "GenericAgent replacement milestone",
        [implementation.channel_id, operations.channel_id],
        decided_by_participant_id="moderator",
      )

      with UnitOfWork(conn) as uow:
        metadata = uow.artifacts.get_metadata(artifact.artifact_id)
      messages = fabric.list_messages(implementation.channel_id)

      self.assertEqual(artifact.media_type, "application/vnd.meadow.cross-channel-decision+json")
      self.assertEqual(metadata["kind"], "cross_channel_decision_artifact")
      self.assertEqual(metadata["payload"]["channel_count"], 2)
      self.assertEqual(metadata["payload"]["message_count"], 3)
      self.assertEqual(metadata["payload"]["participants"], ["builder", "operator", "reviewer"])
      self.assertEqual(metadata["payload"]["channels"][0]["channel_id"], implementation.channel_id)
      self.assertEqual(metadata["payload"]["channels"][1]["payload"]["message_count"], 1)
      self.assertEqual(messages[-1].content["type"], "cross_channel_decision_artifact")
      self.assertEqual(messages[-1].content["artifact_id"], artifact.artifact_id)
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

  def test_observer_request_pause_can_pause_runtime_run(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      workflow = WorkflowSpec(
        workflow_id="wf_observed",
        version="0.1.0",
        name="observed",
        input_schema={},
        output_schema={},
        nodes=[NodeSpec(node_id="start", kind="test")],
        edges=[],
        start_node_id="start",
      )
      registry = NodeExecutorRegistry()
      registry.register("test", lambda: FunctionNodeExecutor(lambda ctx: NodeResult()))
      engine = RuntimeEngine(uow_factory, registry)
      run = engine.create_run(workflow, run_id="run_observed")
      running = asyncio.run(engine.run_until_waiting(workflow, run.run_id, max_steps=1))
      self.assertEqual(running.status, RunStatus.RUNNING)

      finding = ObserverService(uow_factory, pause_controller=engine).request_pause(
        observer_id="observer_1",
        target_run_id=run.run_id,
        message="unsafe operation detected",
      )

      with UnitOfWork(conn) as uow:
        paused = uow.states.get(run.run_id)
        events = uow.events.list_by_run(run.run_id)
        checkpoint = uow.checkpoints.latest_for_run(run.run_id)
        findings = uow.interactions.list_findings(run.run_id)

      pause_events = [event for event in events if event.event_type is RuntimeEventType.RUN_PAUSED]
      self.assertEqual(paused.status, RunStatus.PAUSED)
      self.assertIsNotNone(checkpoint)
      self.assertEqual(checkpoint.state.status, RunStatus.PAUSED)
      self.assertEqual(pause_events[-1].payload["source"], "observer")
      self.assertEqual(pause_events[-1].payload["finding_id"], finding.finding_id)
      self.assertEqual(findings[0].finding_id, finding.finding_id)
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
