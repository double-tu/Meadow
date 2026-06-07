"""Application service for host-visible tool call control requests."""

from __future__ import annotations

from dataclasses import dataclass

from agent_kernel.capabilities import CapabilityRuntime
from agent_kernel.domain.events import RuntimeEvent, RuntimeEventType
from agent_kernel.domain.states import ToolCallStatus
from agent_kernel.domain.tool_call import ToolCallRecord


TERMINAL_TOOL_CALL_STATUSES = {
  ToolCallStatus.CANCELLED,
  ToolCallStatus.KILLED,
  ToolCallStatus.SUCCEEDED,
  ToolCallStatus.FAILED,
}


@dataclass(slots=True)
class ToolCallControlOutcome:
  tool_call: ToolCallRecord
  dispatched: bool
  requested_status: ToolCallStatus


class ToolCallControlService:
  def __init__(self, uow_factory, runtime: CapabilityRuntime | None = None) -> None:
    self._uow_factory = uow_factory
    self._runtime = runtime

  async def cancel(self, tool_call_id: str, grace_seconds: float = 1.0) -> ToolCallControlOutcome:
    record = self._get_tool_call(tool_call_id)
    if record.status in TERMINAL_TOOL_CALL_STATUSES:
      raise ValueError(f"Tool call is already terminal: {record.status.value}")
    self._append_event(
      record,
      RuntimeEventType.TOOL_CALL_CANCEL_REQUESTED,
      {"grace_seconds": grace_seconds},
    )
    if self._runtime is not None:
      controlled = await self._runtime.cancel_tool_call(tool_call_id, grace_seconds=grace_seconds)
      self._append_terminal_event(controlled)
      return ToolCallControlOutcome(
        tool_call=controlled,
        dispatched=True,
        requested_status=controlled.status,
      )
    updated = self._save_status(record, ToolCallStatus.CANCELLING)
    return ToolCallControlOutcome(
      tool_call=updated,
      dispatched=False,
      requested_status=ToolCallStatus.CANCELLING,
    )

  async def kill(self, tool_call_id: str) -> ToolCallControlOutcome:
    record = self._get_tool_call(tool_call_id)
    if record.status in TERMINAL_TOOL_CALL_STATUSES:
      raise ValueError(f"Tool call is already terminal: {record.status.value}")
    self._append_event(record, RuntimeEventType.TOOL_CALL_KILL_REQUESTED, {})
    if self._runtime is not None:
      controlled = await self._runtime.kill_tool_call(tool_call_id)
      self._append_terminal_event(controlled)
      return ToolCallControlOutcome(
        tool_call=controlled,
        dispatched=True,
        requested_status=controlled.status,
      )
    updated = self._save_status(record, ToolCallStatus.KILLING)
    return ToolCallControlOutcome(
      tool_call=updated,
      dispatched=False,
      requested_status=ToolCallStatus.KILLING,
    )

  def _get_tool_call(self, tool_call_id: str) -> ToolCallRecord:
    with self._uow_factory() as uow:
      record = uow.tool_calls.get(tool_call_id)
    if record is None:
      raise KeyError(f"Tool call not found: {tool_call_id}")
    return record

  def _save_status(self, record: ToolCallRecord, status: ToolCallStatus) -> ToolCallRecord:
    updated = ToolCallRecord(
      tool_call_id=record.tool_call_id,
      run_id=record.run_id,
      capability_id=record.capability_id,
      status=status,
      idempotency_key=record.idempotency_key,
      process_id=record.process_id,
      input=record.input,
      output=record.output,
      error=record.error,
      created_at=record.created_at,
    )
    with self._uow_factory() as uow:
      uow.tool_calls.save(updated)
    return updated

  def _append_terminal_event(self, record: ToolCallRecord) -> None:
    if record.status is ToolCallStatus.CANCELLED:
      self._append_event(record, RuntimeEventType.TOOL_CALL_CANCELLED, {})
    elif record.status is ToolCallStatus.KILLED:
      self._append_event(record, RuntimeEventType.TOOL_CALL_KILLED, {})
    elif record.status is ToolCallStatus.FAILED:
      self._append_event(record, RuntimeEventType.TOOL_CALL_FAILED, {})

  def _append_event(
    self,
    record: ToolCallRecord,
    event_type: RuntimeEventType,
    payload: dict[str, object],
  ) -> None:
    with self._uow_factory() as uow:
      uow.events.append(
        RuntimeEvent(
          event_type=event_type,
          run_id=record.run_id,
          payload={
            "tool_call_id": record.tool_call_id,
            "capability_id": record.capability_id,
            **payload,
          },
        )
      )
