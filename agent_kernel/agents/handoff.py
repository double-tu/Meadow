"""Task handoff service for multi-agent collaboration."""

from __future__ import annotations

from dataclasses import replace
from typing import Protocol

from agent_kernel.domain.base import new_id, utc_now
from agent_kernel.domain.identifiers import ArtifactRef
from agent_kernel.domain.interaction import HandoffRecord, InteractionChannel


class HandoffPolicy(Protocol):
  def validate(self, handoff: HandoffRecord, target_channel: InteractionChannel | None) -> None:
    ...


class ChannelMembershipHandoffPolicy:
  def validate(self, handoff: HandoffRecord, target_channel: InteractionChannel | None) -> None:
    if target_channel is None:
      return
    if handoff.to_participant_id not in target_channel.participant_ids:
      raise ValueError(
        f"Target participant {handoff.to_participant_id} is not in channel {target_channel.channel_id}."
      )


class HandoffService:
  def __init__(self, uow_factory, policy: HandoffPolicy | None = None) -> None:
    self._uow_factory = uow_factory
    self._policy = policy or ChannelMembershipHandoffPolicy()

  def request(
    self,
    task_id: str,
    from_participant_id: str,
    to_participant_id: str,
    reason: str,
    state_summary: str,
    expected_output: str,
    source_channel_id: str | None = None,
    target_channel_id: str | None = None,
    constraints: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    artifact_refs: list[ArtifactRef] | None = None,
  ) -> HandoffRecord:
    handoff = HandoffRecord(
      handoff_id=new_id("handoff"),
      task_id=task_id,
      from_participant_id=from_participant_id,
      to_participant_id=to_participant_id,
      reason=reason,
      state_summary=state_summary,
      expected_output=expected_output,
      source_channel_id=source_channel_id,
      target_channel_id=target_channel_id,
      constraints=constraints or [],
      acceptance_criteria=acceptance_criteria or [],
      artifact_refs=artifact_refs or [],
    )
    with self._uow_factory() as uow:
      target_channel = uow.interactions.get_channel(target_channel_id) if target_channel_id else None
      self._policy.validate(handoff, target_channel)
      uow.interactions.save_handoff(handoff)
      if target_channel_id is not None:
        message = self._handoff_message(handoff)
        uow.interactions.save_message(message)
    return handoff

  def accept(self, handoff_id: str) -> HandoffRecord:
    return self._resolve(handoff_id, "accepted")

  def reject(self, handoff_id: str) -> HandoffRecord:
    return self._resolve(handoff_id, "rejected")

  def _resolve(self, handoff_id: str, status: str) -> HandoffRecord:
    with self._uow_factory() as uow:
      handoff = uow.interactions.get_handoff(handoff_id)
      if handoff is None:
        raise KeyError(f"Handoff not found: {handoff_id}")
      updated = replace(handoff, status=status, updated_at=utc_now())  # type: ignore[arg-type]
      uow.interactions.save_handoff(updated)
      return updated

  @staticmethod
  def _handoff_message(handoff: HandoffRecord):
    from agent_kernel.domain.interaction import InteractionMessage

    return InteractionMessage(
      message_id=new_id("interaction_msg"),
      channel_id=handoff.target_channel_id or "",
      sender_participant_id=handoff.from_participant_id,
      content={
        "type": "handoff",
        "handoff_id": handoff.handoff_id,
        "task_id": handoff.task_id,
        "to_participant_id": handoff.to_participant_id,
        "reason": handoff.reason,
        "state_summary": handoff.state_summary,
        "expected_output": handoff.expected_output,
        "constraints": handoff.constraints,
        "acceptance_criteria": handoff.acceptance_criteria,
      },
      artifact_refs=handoff.artifact_refs,
    )
