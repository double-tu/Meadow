"""Capability runtime enforcing policy before adapter execution."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from typing import Any, cast

from agent_kernel.capabilities.adapters.control import ControlResult, ControlTargetKind, ControlWorkbench
from agent_kernel.capabilities.adapters.local import LocalToolExecutor
from agent_kernel.capabilities.adapters.mcp import MCPToolExecutor
from agent_kernel.capabilities.adapters.process import ProcessToolExecutor
from agent_kernel.capabilities.adapters.workbench import WorkbenchClient, WorkbenchCommand
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
    mcp_tools: MCPToolExecutor | None = None,
    control_workbench: ControlWorkbench | None = None,
    workbench_client: WorkbenchClient | None = None,
    uow_factory=None,
  ) -> None:
    self._registry = registry
    self._policy = policy
    self._local_tools = local_tools
    self._process_tools = process_tools
    self._mcp_tools = mcp_tools
    self._control_workbench = control_workbench
    self._workbench_client = workbench_client
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
    if spec.kind not in {"tool", "workbench"}:
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
    started_at = utc_now()
    if spec.kind == "workbench":
      raw_result = await self._call_workbench_adapter(input)
    else:
      raw_result = await self._call_tool_adapter(capability_id, input, tool_call.tool_call_id)
    result = ToolResult.from_value(raw_result).with_context(
      capability_id=capability_id,
      tool_call_id=tool_call.tool_call_id,
      provider=spec.kind,
      started_at=started_at,
      finished_at=utc_now(),
      metadata={"side_effect_level": spec.side_effect_level.value},
    )
    terminal_status = self._terminal_status_from_result(result)
    self._update_tool_call(
      tool_call,
      status=terminal_status,
      output=self._tool_call_output(result),
      error=result.error,
    )
    self._write_audit(
      ctx=ctx,
      capability_id=capability_id,
      decision="result",
      payload={"tool_call_id": tool_call.tool_call_id, "result": result.envelope()},
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
    if self._mcp_tools is not None:
      try:
        return await self._mcp_tools.call(capability_id, input)
      except KeyError:
        pass
    return await self._local_tools.call(capability_id, input)

  async def _call_workbench_adapter(self, input: dict[str, Any]) -> ToolResult:
    if self._is_control_workbench_input(input):
      return await self._call_control_workbench_adapter(input)
    if self._workbench_client is None:
      return ToolResult(
        ok=False,
        error={"type": "workbench_client_not_configured"},
      )
    command_kind = input.get("command_kind") or input.get("kind")
    if not isinstance(command_kind, str) or not command_kind:
      return ToolResult(
        ok=False,
        error={"type": "invalid_workbench_command", "message": "command_kind is required."},
      )
    payload = input.get("payload", {})
    if not isinstance(payload, dict):
      return ToolResult(
        ok=False,
        error={"type": "invalid_workbench_command", "message": "payload must be a dictionary."},
      )
    command_id = input.get("command_id")
    result = await self._workbench_client.execute(
      WorkbenchCommand(
        command_id=command_id if isinstance(command_id, str) and command_id else new_id("workbench_cmd"),
        kind=command_kind,
        payload=payload,
      )
    )
    return ToolResult(ok=result.ok, output=result.output, error=result.error)

  async def _call_control_workbench_adapter(self, input: dict[str, Any]) -> ToolResult:
    if self._control_workbench is None:
      return ToolResult(
        ok=False,
        error={"type": "control_workbench_not_configured"},
      )
    action = input.get("action")
    target_kind = input.get("target_kind")
    target_id = input.get("target_id")
    timeout_seconds = input.get("timeout_seconds")
    try:
      if action == "list_targets":
        kind = target_kind if target_kind in {"browser", "desktop", "mobile"} else None
        targets = [target.to_dict() for target in self._control_workbench.list_targets(kind)]
        return ToolResult(ok=True, output={"targets": targets})
      result = await self._dispatch_control_action(
        action=action,
        target_kind=target_kind,
        target_id=target_id if isinstance(target_id, str) else None,
        payload=input.get("payload", {}),
        timeout_seconds=timeout_seconds if isinstance(timeout_seconds, (int, float)) else None,
      )
    except (TypeError, ValueError) as exc:
      return ToolResult(ok=False, error={"type": "invalid_control_command", "message": str(exc)})
    return ToolResult(ok=result.ok, output=result.output, error=result.error)

  @staticmethod
  def _is_control_workbench_input(input: dict[str, Any]) -> bool:
    return "action" in input or "target_kind" in input or "target_id" in input

  async def _dispatch_control_action(
    self,
    action: Any,
    target_kind: Any,
    target_id: str | None,
    payload: Any,
    timeout_seconds: float | None,
  ) -> ControlResult:
    workbench = self._control_workbench
    if workbench is None:
      raise RuntimeError("Control workbench is not configured.")
    if not isinstance(payload, dict):
      raise TypeError("payload must be a dictionary.")
    if action == "inspect_browser":
      return await workbench.inspect_browser(target_id)
    if action == "execute_js":
      code = payload.get("code")
      if not isinstance(code, str):
        raise ValueError("payload.code is required for execute_js.")
      return await workbench.execute_js(code, target_id, timeout_seconds)
    if action == "navigate":
      url = payload.get("url")
      if not isinstance(url, str):
        raise ValueError("payload.url is required for navigate.")
      return await workbench.navigate(url, target_id, timeout_seconds)
    if action == "screenshot":
      return await workbench.screenshot(self._require_target_kind(target_kind), target_id)
    if action == "click":
      x, y = self._require_xy(payload)
      return await workbench.click(self._require_target_kind(target_kind), x, y, target_id)
    if action == "key":
      key = payload.get("key")
      if not isinstance(key, str):
        raise ValueError("payload.key is required for key.")
      return await workbench.key(self._require_target_kind(target_kind), key, target_id)
    if action == "type_text":
      text = payload.get("text")
      if not isinstance(text, str):
        raise ValueError("payload.text is required for type_text.")
      return await workbench.type_text(self._require_target_kind(target_kind), text, target_id)
    if action == "dump_ui":
      return await workbench.dump_ui(self._require_target_kind(target_kind), target_id)
    if action == "tap":
      x, y = self._require_xy(payload)
      return await workbench.tap(x, y, target_id)
    raise ValueError(f"Unsupported control action: {action}")

  @staticmethod
  def _require_target_kind(value: Any) -> ControlTargetKind:
    if value not in {"browser", "desktop", "mobile"}:
      raise ValueError("target_kind must be one of browser, desktop, mobile.")
    return cast(ControlTargetKind, value)

  @staticmethod
  def _require_xy(payload: dict[str, Any]) -> tuple[int, int]:
    x = payload.get("x")
    y = payload.get("y")
    if not isinstance(x, int) or not isinstance(y, int):
      raise ValueError("payload.x and payload.y are required integer coordinates.")
    return x, y

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

  @staticmethod
  def _tool_call_output(result: ToolResult) -> dict[str, Any]:
    output = dict(result.output)
    output["_tool_result"] = {
      "result_id": result.result_id,
      "status": result.status,
      "capability_id": result.capability_id,
      "tool_call_id": result.tool_call_id,
      "provider": result.provider,
      "finished_at": result.finished_at.isoformat(),
      "metadata": result.metadata,
    }
    return output

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
