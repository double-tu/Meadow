"""Command line host."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from agent_kernel.app.tool_call_control import ToolCallControlService
from agent_kernel.config import LLMConfig
from agent_kernel.domain import EdgeSpec, NodeSpec, WorkflowSpec
from agent_kernel.domain.context import ModelContext
from agent_kernel.domain.workflow import ExecutionCommand, NodeContext, NodeResult
from agent_kernel.evaluation import ReplayService
from agent_kernel.hosts.dto import ok_response
from agent_kernel.models import ModelGateway, OpenAICompatibleProvider
from agent_kernel.observability import TraceService
from agent_kernel.persistence import connect_sqlite
from agent_kernel.policy import ApprovalService, HumanInterventionService
from agent_kernel.runtime import RuntimeEngine, unit_of_work_factory
from agent_kernel.workflow import FunctionNodeExecutor, NodeExecutorRegistry


def build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(prog="agent-kernel")
  parser.add_argument("--db", default="agent_kernel.sqlite", help="SQLite database path.")
  parser.add_argument("--config", default=None, help="Path to agent-kernel TOML/JSON config.")
  subcommands = parser.add_subparsers(dest="command", required=True)

  sample_run = subcommands.add_parser("sample-run")
  sample_run.add_argument("--run-id", default=None)
  sample_run.add_argument("--text", default="hello")

  inspect = subcommands.add_parser("inspect")
  inspect.add_argument("run_id")

  replay = subcommands.add_parser("replay")
  replay.add_argument("run_id")

  approve = subcommands.add_parser("approve")
  approve.add_argument("approval_id")

  reject = subcommands.add_parser("reject")
  reject.add_argument("approval_id")

  cancel = subcommands.add_parser("cancel")
  cancel.add_argument("run_id")
  cancel.add_argument("--reason", default="user requested cancel")

  cancel_tool_call = subcommands.add_parser("cancel-tool-call")
  cancel_tool_call.add_argument("tool_call_id")
  cancel_tool_call.add_argument("--grace-seconds", type=float, default=1.0)

  kill_tool_call = subcommands.add_parser("kill-tool-call")
  kill_tool_call.add_argument("tool_call_id")

  intervene = subcommands.add_parser("intervene")
  intervene.add_argument("run_id")
  intervene.add_argument("--content", required=True)
  intervene.add_argument("--type", default="correction")
  intervene.add_argument("--mode", default="continue_next_turn")
  intervene.add_argument("--priority", default="high")
  intervene.add_argument("--task-id", default=None)
  intervene.add_argument("--memory-scope", default=None)

  llm_smoke = subcommands.add_parser("llm-smoke")
  llm_smoke.add_argument("--prompt", default="Say hello in one short sentence.")
  llm_smoke.add_argument("--model", default=None)
  llm_smoke.add_argument("--provider", default=None)

  return parser


def main(argv: list[str] | None = None) -> int:
  parser = build_parser()
  args = parser.parse_args(argv)
  conn = connect_sqlite(Path(args.db))
  try:
    response = asyncio.run(_dispatch(args, conn))
    print(json.dumps(response, ensure_ascii=False, sort_keys=True))
    return 0
  finally:
    conn.close()


async def _dispatch(args: argparse.Namespace, conn) -> dict[str, Any]:
  uow_factory = unit_of_work_factory(conn)
  if args.command == "sample-run":
    workflow = _sample_workflow()
    registry = NodeExecutorRegistry()
    registry.register("echo", lambda: FunctionNodeExecutor(_echo_node))
    registry.register("finish", lambda: FunctionNodeExecutor(lambda ctx: NodeResult(command=ExecutionCommand(type="finish"))))
    engine = RuntimeEngine(uow_factory, registry)
    created = engine.create_run(workflow, input={"text": args.text}, run_id=args.run_id)
    completed = await engine.run_until_waiting(workflow, created.run_id)
    return ok_response(run_id=completed.run_id, status=completed.status.value, variables=completed.variables)
  if args.command == "inspect":
    timeline = TraceService(uow_factory).build_timeline(args.run_id)
    return ok_response(run_id=args.run_id, entries=[entry.to_dict() for entry in timeline.entries])
  if args.command == "replay":
    replay = ReplayService(uow_factory).exact_replay(args.run_id)
    return ok_response(
      run_id=args.run_id,
      event_count=len(replay.events),
      status=replay.final_state.status.value if replay.final_state else None,
      replayed_external_calls=replay.replayed_external_calls,
    )
  if args.command == "approve":
    grant = ApprovalService(uow_factory).approve(args.approval_id)
    return ok_response(grant=grant.to_dict())
  if args.command == "reject":
    approval = ApprovalService(uow_factory).reject(args.approval_id)
    return ok_response(approval=approval.to_dict())
  if args.command == "cancel":
    engine = RuntimeEngine(uow_factory, NodeExecutorRegistry())
    state = engine.cancel_run(args.run_id, args.reason)
    return ok_response(run_id=args.run_id, status=state.status.value)
  if args.command == "cancel-tool-call":
    outcome = await ToolCallControlService(uow_factory).cancel(
      args.tool_call_id,
      grace_seconds=args.grace_seconds,
    )
    return ok_response(
      tool_call=outcome.tool_call.to_dict(),
      dispatched=outcome.dispatched,
      requested_status=outcome.requested_status.value,
    )
  if args.command == "kill-tool-call":
    outcome = await ToolCallControlService(uow_factory).kill(args.tool_call_id)
    return ok_response(
      tool_call=outcome.tool_call.to_dict(),
      dispatched=outcome.dispatched,
      requested_status=outcome.requested_status.value,
    )
  if args.command == "intervene":
    outcome = HumanInterventionService(uow_factory).apply(
      run_id=args.run_id,
      content=args.content,
      intervention_type=args.type,
      apply_mode=args.mode,
      priority=args.priority,
      task_id=args.task_id,
      memory_scope=args.memory_scope,
    )
    return ok_response(
      run_id=args.run_id,
      intervention=outcome.intervention.to_dict(),
      run_status=outcome.run_status.value if hasattr(outcome.run_status, "value") else outcome.run_status,
      memory_id=outcome.memory_id,
      interrupted_step_id=outcome.interrupted_step_id,
    )
  if args.command == "llm-smoke":
    config = LLMConfig.load(args.config)
    provider_name = args.provider or config.provider
    model_ref = args.model or config.model
    gateway = ModelGateway()
    gateway.register_provider(
      provider_name,
      OpenAICompatibleProvider(
        api_key=config.api_key,
        base_url=config.base_url,
        timeout_seconds=config.timeout_seconds,
      ),
    )
    result = await gateway.complete(
      provider_name=provider_name,
      model_ref=model_ref,
      context=ModelContext(messages=[{"role": "user", "content": args.prompt}]),
    )
    return ok_response(provider=provider_name, model=model_ref, result=result)
  raise ValueError(f"Unsupported command: {args.command}")


def _sample_workflow() -> WorkflowSpec:
  return WorkflowSpec(
    workflow_id="wf_sample",
    version="0.1.0",
    name="sample",
    input_schema={},
    output_schema={},
    nodes=[
      NodeSpec(node_id="echo", kind="echo"),
      NodeSpec(node_id="finish", kind="finish"),
    ],
    edges=[EdgeSpec(from_node="echo", to_node="finish")],
    start_node_id="echo",
  )


def _echo_node(ctx: NodeContext) -> NodeResult:
  return NodeResult(state_patch={"echo": ctx.input.get("text")})


if __name__ == "__main__":
  raise SystemExit(main())
