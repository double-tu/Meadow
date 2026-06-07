"""Tool node executor using CapabilityRuntime."""

from agent_kernel.capabilities.runtime import CapabilityCallContext, CapabilityRuntime
from agent_kernel.domain.workflow import ExecutionCommand, NodeContext, NodeResult
from agent_kernel.policy.approval import ApprovalService
from agent_kernel.policy.engine import PolicyDecisionType


class ToolNodeExecutor:
  def __init__(
    self,
    capability_runtime: CapabilityRuntime,
    approval_service: ApprovalService | None = None,
  ) -> None:
    self._capability_runtime = capability_runtime
    self._approval_service = approval_service

  async def execute(self, ctx: NodeContext) -> NodeResult:
    capability_id = ctx.node.capability_ref
    if capability_id is None:
      return NodeResult(
        command=ExecutionCommand(
          type="fail",
          payload={"reason": "Tool node missing capability_ref."},
        )
      )
    outcome = await self._capability_runtime.call(
      capability_id=capability_id,
      input=ctx.input,
      ctx=CapabilityCallContext(
        run_id=ctx.run_id,
        idempotency_key=ctx.idempotency_key,
      ),
    )
    if outcome.decision.type is PolicyDecisionType.REQUIRE_APPROVAL:
      approval_id = None
      if self._approval_service is not None:
        approval = self._approval_service.request_tool_approval(
          run_id=ctx.run_id,
          capability_id=capability_id,
          reason=outcome.decision.reason,
          payload={"input": ctx.input, "node_id": ctx.node.node_id},
        )
        approval_id = approval.approval_id
      return NodeResult(
        command=ExecutionCommand(
          type="request_approval",
          target=capability_id,
          payload={"reason": outcome.decision.reason, "approval_id": approval_id},
        )
      )
    if outcome.result is None:
      return NodeResult(
        command=ExecutionCommand(
          type="fail",
          payload={"reason": "Capability returned no result."},
        )
      )
    if not outcome.result.ok:
      return NodeResult(
        command=ExecutionCommand(
          type="fail",
          payload={"reason": outcome.result.error},
        ),
        artifact_refs=outcome.result.artifact_refs,
      )
    return NodeResult(
      state_patch=outcome.result.output,
      events=outcome.result.events,
      artifact_refs=outcome.result.artifact_refs,
    )
