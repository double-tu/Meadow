"""Group chat orchestration."""

from agent_kernel.agents.interaction_fabric import InteractionFabric
from agent_kernel.domain.base import new_id
from agent_kernel.domain.interaction import (
  DiscussionTurn,
  GroupChatSession,
  SpeakerPolicy,
  SpeakerPolicyType,
)


class GroupChatService:
  def __init__(self, uow_factory, fabric: InteractionFabric) -> None:
    self._uow_factory = uow_factory
    self._fabric = fabric

  def create(
    self,
    thread_id: str,
    topic: str,
    participant_ids: list[str],
    turn_limit: int = 10,
  ) -> GroupChatSession:
    session = GroupChatSession(
      group_chat_id=new_id("group_chat"),
      thread_id=thread_id,
      topic=topic,
      participant_ids=participant_ids,
      speaker_policy=SpeakerPolicy(type=SpeakerPolicyType.ROUND_ROBIN),
      turn_limit=turn_limit,
      status="running",
    )
    with self._uow_factory() as uow:
      uow.interactions.save_group_chat(session)
    return session

  def next_speaker(self, session: GroupChatSession) -> str:
    with self._uow_factory() as uow:
      turns = uow.interactions.list_turns(session.group_chat_id)
    return session.participant_ids[len(turns) % len(session.participant_ids)]

  def add_turn(
    self,
    session: GroupChatSession,
    channel_id: str,
    content: dict[str, object],
  ) -> DiscussionTurn:
    speaker = self.next_speaker(session)
    message = self._fabric.send_message(channel_id, speaker, content)
    with self._uow_factory() as uow:
      turns = uow.interactions.list_turns(session.group_chat_id)
      turn = DiscussionTurn(
        turn_id=new_id("turn"),
        group_chat_id=session.group_chat_id,
        speaker_participant_id=speaker,
        message_id=message.message_id,
        turn_index=len(turns),
      )
      uow.interactions.save_turn(turn)
    return turn

