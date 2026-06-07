"""Group chat orchestration."""

from dataclasses import dataclass, replace
from typing import Any, Protocol

from agent_kernel.agents.interaction_fabric import InteractionFabric
from agent_kernel.domain.base import new_id
from agent_kernel.domain.identifiers import ArtifactRef
from agent_kernel.domain.interaction import (
  DiscussionTurn,
  GroupChatSession,
  InteractionMessage,
  SpeakerPolicy,
  SpeakerPolicyType,
)


class GroupChatService:
  def __init__(
    self,
    uow_factory,
    fabric: InteractionFabric,
    speaker_selector: "SpeakerSelector | None" = None,
  ) -> None:
    self._uow_factory = uow_factory
    self._fabric = fabric
    self._speaker_selector = speaker_selector or CompositeSpeakerSelector()

  def create(
    self,
    thread_id: str,
    topic: str,
    participant_ids: list[str],
    turn_limit: int = 10,
    speaker_policy: SpeakerPolicy | None = None,
    moderator_participant_id: str | None = None,
  ) -> GroupChatSession:
    session = GroupChatSession(
      group_chat_id=new_id("group_chat"),
      thread_id=thread_id,
      topic=topic,
      participant_ids=participant_ids,
      speaker_policy=speaker_policy or SpeakerPolicy(type=SpeakerPolicyType.ROUND_ROBIN),
      moderator_participant_id=moderator_participant_id,
      turn_limit=turn_limit,
      status="running",
    )
    with self._uow_factory() as uow:
      uow.interactions.save_group_chat(session)
    return session

  def next_speaker(self, session: GroupChatSession) -> str:
    with self._uow_factory() as uow:
      turns = uow.interactions.list_turns(session.group_chat_id)
    return self._speaker_selector.select(session, turns, None).speaker_participant_id

  def add_turn(
    self,
    session: GroupChatSession,
    channel_id: str,
    content: dict[str, object],
  ) -> DiscussionTurn:
    with self._uow_factory() as uow:
      turns = uow.interactions.list_turns(session.group_chat_id)
    selection = self._speaker_selector.select(session, turns, content)
    speaker = selection.speaker_participant_id
    message = self._fabric.send_message(channel_id, speaker, content)
    with self._uow_factory() as uow:
      turn = DiscussionTurn(
        turn_id=new_id("turn"),
        group_chat_id=session.group_chat_id,
        speaker_participant_id=speaker,
        message_id=message.message_id,
        turn_index=len(turns),
        selected_by=selection.selected_by,
        rationale=selection.rationale,
      )
      uow.interactions.save_turn(turn)
    return turn


@dataclass(slots=True)
class SpeakerSelection:
  speaker_participant_id: str
  selected_by: str | None = None
  rationale: str | None = None


class SpeakerSelector(Protocol):
  def select(
    self,
    session: GroupChatSession,
    turns: list[DiscussionTurn],
    content: dict[str, object] | None = None,
  ) -> SpeakerSelection:
    ...


class RoundRobinSpeakerSelector:
  def select(
    self,
    session: GroupChatSession,
    turns: list[DiscussionTurn],
    content: dict[str, object] | None = None,
  ) -> SpeakerSelection:
    return SpeakerSelection(
      speaker_participant_id=session.participant_ids[len(turns) % len(session.participant_ids)],
      selected_by="round_robin",
      rationale="Next participant selected by turn index.",
    )


class FreeForAllSpeakerSelector:
  def select(
    self,
    session: GroupChatSession,
    turns: list[DiscussionTurn],
    content: dict[str, object] | None = None,
  ) -> SpeakerSelection:
    requested = (content or {}).get("speaker_participant_id")
    if isinstance(requested, str) and requested in session.participant_ids:
      return SpeakerSelection(
        speaker_participant_id=requested,
        selected_by="free_for_all",
        rationale="Speaker provided by caller.",
      )
    least_recent = self._least_recent_participant(session.participant_ids, turns)
    return SpeakerSelection(
      speaker_participant_id=least_recent,
      selected_by="free_for_all",
      rationale="No valid speaker requested; selected least recent participant.",
    )

  @staticmethod
  def _least_recent_participant(participant_ids: list[str], turns: list[DiscussionTurn]) -> str:
    for participant_id in participant_ids:
      if all(turn.speaker_participant_id != participant_id for turn in turns):
        return participant_id
    last_index = {
      turn.speaker_participant_id: turn.turn_index
      for turn in turns
      if turn.speaker_participant_id in participant_ids
    }
    return min(participant_ids, key=lambda participant_id: last_index.get(participant_id, -1))


class ModeratorSpeakerSelector:
  def select(
    self,
    session: GroupChatSession,
    turns: list[DiscussionTurn],
    content: dict[str, object] | None = None,
  ) -> SpeakerSelection:
    requested = (content or {}).get("selected_speaker_participant_id")
    if isinstance(requested, str) and requested in session.participant_ids:
      return SpeakerSelection(
        speaker_participant_id=requested,
        selected_by=session.moderator_participant_id or "moderator",
        rationale=str((content or {}).get("selection_rationale") or "Selected by moderator."),
      )
    moderator = session.moderator_participant_id
    if moderator in session.participant_ids:
      return SpeakerSelection(
        speaker_participant_id=moderator,
        selected_by="moderator_select",
        rationale="No target speaker supplied; moderator speaks.",
      )
    return RoundRobinSpeakerSelector().select(session, turns, content)


class CompositeSpeakerSelector:
  def __init__(self) -> None:
    self._round_robin = RoundRobinSpeakerSelector()
    self._free_for_all = FreeForAllSpeakerSelector()
    self._moderator = ModeratorSpeakerSelector()

  def select(
    self,
    session: GroupChatSession,
    turns: list[DiscussionTurn],
    content: dict[str, object] | None = None,
  ) -> SpeakerSelection:
    policy_type = session.speaker_policy.type
    if policy_type is SpeakerPolicyType.FREE_FOR_ALL:
      return self._free_for_all.select(session, turns, content)
    if policy_type is SpeakerPolicyType.MODERATOR_SELECT:
      return self._moderator.select(session, turns, content)
    return self._round_robin.select(session, turns, content)


class DiscussionSummarizer(Protocol):
  def summarize(
    self,
    topic: str,
    messages: list[InteractionMessage],
  ) -> dict[str, Any]:
    ...


class DeterministicDiscussionSummarizer:
  """Local summarizer used before wiring model-backed discussion synthesis."""

  def summarize(
    self,
    topic: str,
    messages: list[InteractionMessage],
  ) -> dict[str, Any]:
    contributions = [
      {
        "message_id": message.message_id,
        "participant_id": message.sender_participant_id,
        "content": message.content,
      }
      for message in messages
    ]
    decisions = [
      message.content
      for message in messages
      if message.content.get("type") in {"decision", "connector_turn"}
    ]
    return {
      "topic": topic,
      "message_count": len(messages),
      "participants": sorted({message.sender_participant_id for message in messages}),
      "summary": self._summary_text(messages),
      "decision": decisions[-1] if decisions else None,
      "contributions": contributions,
    }

  @staticmethod
  def _summary_text(messages: list[InteractionMessage]) -> str:
    if not messages:
      return "No discussion messages."
    fragments: list[str] = []
    for message in messages[-5:]:
      text = message.content.get("text") or message.content.get("summary") or message.content.get("type")
      fragments.append(f"{message.sender_participant_id}: {text}")
    return "\n".join(fragments)


class DecisionArtifactService:
  def __init__(
    self,
    uow_factory,
    fabric: InteractionFabric,
    summarizer: DiscussionSummarizer | None = None,
  ) -> None:
    self._uow_factory = uow_factory
    self._fabric = fabric
    self._summarizer = summarizer or DeterministicDiscussionSummarizer()

  def create_for_group_chat(
    self,
    session: GroupChatSession,
    channel_id: str,
    decided_by_participant_id: str | None = None,
  ) -> ArtifactRef:
    messages = self._fabric.list_messages(channel_id)
    payload = self._summarizer.summarize(session.topic, messages)
    artifact_ref = ArtifactRef(
      artifact_id=new_id("decision_artifact"),
      uri=f"artifact://decision/{session.group_chat_id}",
      media_type="application/vnd.meadow.decision+json",
    )
    with self._uow_factory() as uow:
      uow.artifacts.save(
        artifact_ref,
        metadata={
          "kind": "decision_artifact",
          "group_chat_id": session.group_chat_id,
          "channel_id": channel_id,
          "decided_by_participant_id": decided_by_participant_id,
          "payload": payload,
        },
      )
      updated = replace(
        session,
        decision_artifact_ref=artifact_ref,
        status="completed",
      )
      uow.interactions.save_group_chat(updated)
    if decided_by_participant_id is not None:
      self._fabric.send_message(
        channel_id,
        decided_by_participant_id,
        {
          "type": "decision_artifact",
          "artifact_id": artifact_ref.artifact_id,
          "summary": payload["summary"],
        },
      )
    return artifact_ref

  def create_cross_channel(
    self,
    topic: str,
    channel_ids: list[str],
    decided_by_participant_id: str | None = None,
  ) -> ArtifactRef:
    channel_summaries: list[dict[str, Any]] = []
    all_messages: list[InteractionMessage] = []
    for channel_id in channel_ids:
      messages = self._fabric.list_messages(channel_id)
      all_messages.extend(messages)
      channel_summaries.append(
        {
          "channel_id": channel_id,
          "payload": self._summarizer.summarize(topic, messages),
        }
      )
    payload = {
      "topic": topic,
      "channel_count": len(channel_ids),
      "message_count": len(all_messages),
      "participants": sorted({message.sender_participant_id for message in all_messages}),
      "summary": self._summarizer.summarize(topic, all_messages)["summary"],
      "channels": channel_summaries,
    }
    artifact_ref = ArtifactRef(
      artifact_id=new_id("cross_channel_decision"),
      uri=f"artifact://decision/cross-channel/{new_id('bundle')}",
      media_type="application/vnd.meadow.cross-channel-decision+json",
    )
    with self._uow_factory() as uow:
      uow.artifacts.save(
        artifact_ref,
        metadata={
          "kind": "cross_channel_decision_artifact",
          "channel_ids": channel_ids,
          "decided_by_participant_id": decided_by_participant_id,
          "payload": payload,
        },
      )
    if decided_by_participant_id is not None and channel_ids:
      self._fabric.send_message(
        channel_ids[0],
        decided_by_participant_id,
        {
          "type": "cross_channel_decision_artifact",
          "artifact_id": artifact_ref.artifact_id,
          "summary": payload["summary"],
          "channel_ids": channel_ids,
        },
      )
    return artifact_ref
