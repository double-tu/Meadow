"""Capability runtime enforcing policy before adapter execution."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from typing import Any

from agent_kernel.capabilities.adapters.local import LocalToolExecutor
from agent_kernel.capabilities.adapters.process import ProcessToolExecutor
from agent_kernel.capabilities.registry import CapabilityRegistry
from agent_kernel.domain.base import new_id, utc_now
from agent_kernel.domain.capability import ToolResult
from agent_kernel.domain.states import ToolCallStatus
from agent_kernel.domain.tool_call import ToolCallRecord
from agent_kernel.persistence.audit_store import AuditRecord
from agent_kernel.policy.engine import PolicyDecision, PolicyDecisionType, PolicyEngine


@dataclass(slots=True)
class CapabilityCallContext:
  run_id: str
  agent_id: str | None = None
  task_id: str | None = None
  idempotency_key: str | None = None


@dataclass(slots=True)
class CapabilityCallOutcome:
  result: ToolResult | None
  decision: PolicyDecision

  @property
  def requires_approval(self) -> bool:
    return self.decision.type is PolicyDecisionType.REQUIRE_APPROVAL


class CapabilityRuntime:
  def __init__(
    self,
    registry: CapabilityRegistry,
    policy: PolicyEngine,
    local_tools: LocalToolExecutor,
    process_tools: ProcessToolExecutor | None = None,
    uow_factory=None,
  ) -> None:
    self._registry = registry
    self._policy = policy
    self._local_tools = local_tools
    self._process_tools = process_tools
    self._uow_factory = uow_factory

  async def call(
    self,
    capability_id: str,
    input: dict[str, Any],
    ctx: CapabilityCallContext,
  ) -> CapabilityCallOutcome:
    spec = self._registry.get(capability_id)
    tool_call = self._create_tool_call(capability_id, input, ctx)
    decision = self._policy.decide(
      spec,
      run_id=ctx.run_id,
      agent_id=ctx.agent_id,
      task_id=ctx.task_id,
    )
    self._write_audit(
      ctx=ctx,
      capability_id=capability_id,
      decision=decision.type.value,
      payload={"input": input, "reason": decision.reason},
    )
    if decision.type is PolicyDecisionType.DENY:
      self._update_tool_call(
        tool_call,
        status=ToolCallStatus.FAILED,
        error={"type": "policy_denied", "message": decision.reason},
      )
      return CapabilityCallOutcome(
        result=ToolResult(ok=False, error={"type": "policy_denied", "message": decision.reason}),
        decision=decision,
      )
    if decision.type is PolicyDecisionType.REQUIRE_APPROVAL:
      self._update_tool_call(tool_call, status=ToolCallStatus.AWAITING_APPROVAL)
      return CapabilityCallOutcome(result=None, decision=decision)
    if spec.kind != "tool":
      self._update_tool_call(
        tool_call,
        status=ToolCallStatus.FAILED,
        error={"type": "unsupported_capability_kind", "kind": spec.kind},
      )
      return CapabilityCallOutcome(
        result=ToolResult(ok=False, error={"type": "unsupported_capability_kind", "kind": spec.kind}),
        decision=decision,
      )
    self._update_tool_call(tool_call, status=ToolCallStatus.RUNNING)
    result = await self._call_tool_adapter(capability_id, input, tool_call.tool_call_id)
    terminal_status = self._terminal_status_from_result(result)
    self._update_tool_call(
      tool_call,
      status=terminal_status,
      output=result.output,
      error=result.error,
    )
    return CapabilityCallOutcome(result=result, decision=decision)

  async def cancel_tool_call(self, tool_call_id: str, grace_seconds: float = 1.0) -> ToolCallRecord:
    if self._process_tools is None:
      raise RuntimeError("Process tool executor is not configured.")
    record = self._get_tool_call(tool_call_id)
    self._update_tool_call(record, status=ToolCallStatus.CANCELLING)
    status = await self._process_tools.cancel(tool_call_id, grace_seconds=grace_seconds)
    terminal_status = ToolCallStatus.CANCELLED if status == "cancelled" else ToolCallStatus.KILLED
    return self._update_tool_call(record, status=terminal_status)

  async def kill_tool_call(self, tool_call_id: str) -> ToolCallRecord:
    if self._process_tools is None:
      raise RuntimeError("Process tool executor is not configured.")
    record = self._get_tool_call(tool_call_id)
    self._update_tool_call(record, status=ToolCallStatus.KILLING)
    await self._process_tools.kill(tool_call_id)
    return self._update_tool_call(record, status=ToolCallStatus.KILLED)

  async def _call_tool_adapter(
    self,
    capability_id: str,
    input: dict[str, Any],
    tool_call_id: str,
  ) -> ToolResult:
    if self._process_tools is not None:
      try:
        return await self._process_tools.call(capability_id, input, tool_call_id=tool_call_id)
      except KeyError:
        pass
    return await self._local_tools.call(capability_id, input)

  @staticmethod
  def _terminal_status_from_result(result: ToolResult) -> ToolCallStatus:
    if result.ok:
      return ToolCallStatus.SUCCEEDED
    error_type = result.error.get("type") if result.error else None
    if error_type == "cancelled":
      return ToolCallStatus.CANCELLED
    if error_type == "killed":
      return ToolCallStatus.KILLED
    return ToolCallStatus.FAILED

  def _create_tool_call(
    self,
    capability_id: str,
    input: dict[str, Any],
    ctx: CapabilityCallContext,
  ) -> ToolCallRecord:
    record = ToolCallRecord(
      tool_call_id=new_id("tool_call"),
      run_id=ctx.run_id,
      capability_id=capability_id,
      status=ToolCallStatus.REQUESTED,
      idempotency_key=ctx.idempotency_key,
      input=input,
    )
    if self._uow_factory is not None:
      with self._uow_factory() as uow:
        uow.tool_calls.save(record)
    return record

  def _get_tool_call(self, tool_call_id: str) -> ToolCallRecord:
    if self._uow_factory is None:
      raise RuntimeError("UnitOfWork factory is required for tool call control.")
    with self._uow_factory() as uow:
      record = uow.tool_calls.get(tool_call_id)
    if record is None:
      raise KeyError(f"Tool call not found: {tool_call_id}")
    return record

  def _update_tool_call(
    self,
    record: ToolCallRecord,
    status: ToolCallStatus,
    output: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
  ) -> ToolCallRecord:
    updated = replace(
      record,
      status=status,
      output=output or record.output,
      error=error,
      updated_at=utc_now(),
    )
    if self._uow_factory is not None:
      with self._uow_factory() as uow:
        uow.tool_calls.save(updated)
    return updated

  def _write_audit(
    self,
    ctx: CapabilityCallContext,
    capability_id: str,
    decision: str,
    payload: dict[str, Any],
  ) -> None:
    if self._uow_factory is None:
      return
    with self._uow_factory() as uow:
      uow.audit.add(
        AuditRecord.create(
          action="capability.call.policy_check",
          target_ref=capability_id,
          run_id=ctx.run_id,
          actor_id=ctx.agent_id,
          decision=decision,
          payload=payload,
        )
      )
