"""Interaction channel service."""

from agent_kernel.domain.base import new_id
from agent_kernel.domain.interaction import (
  ChannelMode,
  InteractionChannel,
  InteractionMessage,
  InteractionParticipant,
)


class InteractionFabric:
  def __init__(self, uow_factory) -> None:
    self._uow_factory = uow_factory

  def add_participant(self, participant: InteractionParticipant) -> InteractionParticipant:
    with self._uow_factory() as uow:
      uow.interactions.save_participant(participant)
    return participant

  def create_channel(
    self,
    topic: str,
    participant_ids: list[str],
    mode: ChannelMode | str = ChannelMode.AD_HOC,
  ) -> InteractionChannel:
    channel = InteractionChannel(
      channel_id=new_id("channel"),
      topic=topic,
      mode=mode,
      participant_ids=participant_ids,
    )
    with self._uow_factory() as uow:
      uow.interactions.save_channel(channel)
    return channel

  def send_message(
    self,
    channel_id: str,
    sender_participant_id: str,
    content: dict[str, object],
  ) -> InteractionMessage:
    message = InteractionMessage(
      message_id=new_id("interaction_msg"),
      channel_id=channel_id,
      sender_participant_id=sender_participant_id,
      content=content,
    )
    with self._uow_factory() as uow:
      uow.interactions.save_message(message)
    return message

  def list_messages(self, channel_id: str) -> list[InteractionMessage]:
    with self._uow_factory() as uow:
      return uow.interactions.list_messages(channel_id)

