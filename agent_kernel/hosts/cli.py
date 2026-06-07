"""Command line host."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from agent_kernel.agents import (
  AgentDelegationBroker,
  DelegationMCPServer,
  ProductCLIConnectorFactory,
  load_product_cli_connector_specs,
  run_delegation_mcp_stdio,
)
from agent_kernel.app.tool_call_control import ToolCallControlService
from agent_kernel.app.control_plane import ControlPlaneService
from agent_kernel.app.mcp_config import MCPConfigService
from agent_kernel.app.scheduled_tasks import ScheduledTaskService
from agent_kernel.config import LLMConfig, load_config_dict
from agent_kernel.domain import EdgeSpec, NodeSpec, WorkflowSpec
from agent_kernel.domain.context import ModelContext
from agent_kernel.domain.workflow import ExecutionCommand, NodeContext, NodeResult
from agent_kernel.evaluation import ReplayService
from agent_kernel.hosts.dto import ok_response
from agent_kernel.memory import MemoryEvolutionSettlementService
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

  http = subcommands.add_parser("http")
  http.add_argument("--host", default="127.0.0.1")
  http.add_argument("--port", type=int, default=8080)

  delegate_agent = subcommands.add_parser("delegate-agent")
  delegate_agent.add_argument("--parent-run-id", required=True)
  delegate_agent.add_argument("--connector-id", required=True)
  delegate_agent.add_argument("--task", required=True)
  delegate_agent.add_argument("--agent-type", default=None)
  delegate_agent.add_argument("--parent-agent-id", default=None)
  delegate_agent.add_argument("--parent-session-id", default=None)
  delegate_agent.add_argument("--metadata-json", default=None)
  delegate_agent.add_argument(
    "--detach",
    action="store_true",
    help="Return after starting the task. Intended for long-lived hosts; standalone CLI cannot keep the child process alive.",
  )

  delegation_status = subcommands.add_parser("delegation-status")
  delegation_status.add_argument("--parent-run-id", required=True)
  delegation_status.add_argument("--task-id", action="append", default=None)
  delegation_status.add_argument("--wait-ms", type=int, default=None)

  cancel_delegation = subcommands.add_parser("cancel-delegation")
  cancel_delegation.add_argument("--parent-run-id", required=True)
  cancel_delegation.add_argument("--task-id", required=True)
  cancel_delegation.add_argument("--reason", default="user requested delegation cancel")

  recover_delegations = subcommands.add_parser("recover-delegations")
  recover_delegations.add_argument(
    "--reason",
    default="delegation process was not reattached after host restart",
  )

  mcp_delegation = subcommands.add_parser("mcp-delegation-server")
  mcp_delegation.add_argument("--parent-run-id", required=True)

  mcp_list = subcommands.add_parser("mcp-list")
  mcp_list.add_argument("--enabled-only", action="store_true")
  mcp_list.add_argument("--agent-type", default=None)

  mcp_upsert = subcommands.add_parser("mcp-upsert")
  mcp_upsert.add_argument("--json", required=True, help="MCP server definition JSON object.")

  mcp_import = subcommands.add_parser("mcp-import")
  mcp_import.add_argument("--path", default=None, help="Config file path. Defaults to --config.")

  mcp_delete = subcommands.add_parser("mcp-delete")
  mcp_delete.add_argument("name")

  schedule_create = subcommands.add_parser("schedule-create")
  schedule_create.add_argument("--name", required=True)
  schedule_create.add_argument("--kind", required=True, choices=["at", "every", "cron"])
  schedule_create.add_argument("--value", required=True)
  schedule_create.add_argument("--payload-json", required=True)
  schedule_create.add_argument("--task-id", default=None)
  schedule_create.add_argument("--max-triggers", type=int, default=None)

  schedule_list = subcommands.add_parser("schedule-list")
  schedule_list.add_argument("--enabled-only", action="store_true")

  schedule_run_due = subcommands.add_parser("schedule-run-due")
  schedule_run_due.add_argument("--limit", type=int, default=None)

  control_health = subcommands.add_parser("control-health")
  control_health.add_argument("--targets", action="store_true")
  control_health.add_argument("--kind", choices=["browser", "desktop", "mobile"], default=None)

  settle_memory = subcommands.add_parser("settle-memory")
  settle_memory.add_argument("run_id")
  settle_memory.add_argument("--scope", default=None)

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
  if args.command == "http":
    from agent_kernel.hosts.http import serve

    serve(args.db, host=args.host, port=args.port)
    return ok_response(status="http_stopped")
  if args.command == "delegate-agent":
    broker = _build_delegation_broker(args, uow_factory)
    metadata = _metadata_json(args.metadata_json)
    report = await broker.delegate(
      parent_run_id=args.parent_run_id,
      parent_agent_id=args.parent_agent_id,
      parent_session_id=args.parent_session_id,
      connector_id=args.connector_id,
      agent_type=args.agent_type,
      task=args.task,
      metadata=metadata,
    )
    if not args.detach:
      await broker.await_task(report.task_id)
      final_reports = await broker.get_status(parent_run_id=args.parent_run_id, task_ids=[report.task_id])
      report = final_reports[0]
    return ok_response(delegation=report.to_dict(), detached=bool(args.detach))
  if args.command == "delegation-status":
    broker = _build_delegation_broker(args, uow_factory)
    reports = await broker.get_status(
      parent_run_id=args.parent_run_id,
      task_ids=args.task_id,
      wait_ms=args.wait_ms,
    )
    return ok_response(delegations=[report.to_dict() for report in reports])
  if args.command == "cancel-delegation":
    broker = _build_delegation_broker(args, uow_factory)
    report = await broker.cancel(
      parent_run_id=args.parent_run_id,
      task_id=args.task_id,
      reason=args.reason,
    )
    return ok_response(delegation=report.to_dict())
  if args.command == "recover-delegations":
    broker = _build_delegation_broker(args, uow_factory)
    reports = broker.recover_orphaned_running(reason=args.reason)
    return ok_response(recovered=[report.to_dict() for report in reports])
  if args.command == "mcp-delegation-server":
    broker = _build_delegation_broker(args, uow_factory)
    await run_delegation_mcp_stdio(
      DelegationMCPServer(broker, default_parent_run_id=args.parent_run_id)
    )
    return ok_response(status="mcp_delegation_server_stopped")
  if args.command == "mcp-list":
    servers = MCPConfigService(uow_factory).list_servers(
      enabled_only=args.enabled_only,
      agent_type=args.agent_type,
    )
    return ok_response(mcp_servers=[server.to_dict() for server in servers])
  if args.command == "mcp-upsert":
    payload = json.loads(args.json)
    if not isinstance(payload, dict):
      raise ValueError("--json must decode to an object.")
    server = MCPConfigService(uow_factory).upsert_server(payload)
    return ok_response(mcp_server=server.to_dict())
  if args.command == "mcp-import":
    path = args.path or args.config
    if not path:
      raise ValueError("mcp-import requires --path or global --config.")
    servers = MCPConfigService(uow_factory).import_config(load_config_dict(path))
    return ok_response(mcp_servers=[server.to_dict() for server in servers])
  if args.command == "mcp-delete":
    deleted = MCPConfigService(uow_factory).delete_server(args.name)
    return ok_response(name=args.name, deleted=deleted)
  if args.command == "schedule-create":
    payload = json.loads(args.payload_json)
    if not isinstance(payload, dict):
      raise ValueError("--payload-json must decode to an object.")
    scheduled = ScheduledTaskService(uow_factory, _CliScheduledTaskLauncher(uow_factory)).create(
      {
        "task_id": args.task_id,
        "name": args.name,
        "schedule_kind": args.kind,
        "schedule_value": args.value,
        "payload": payload,
        "max_triggers": args.max_triggers,
      }
    )
    return ok_response(scheduled_task=scheduled.to_dict())
  if args.command == "schedule-list":
    tasks = ScheduledTaskService(uow_factory, _CliScheduledTaskLauncher(uow_factory)).list(
      enabled_only=args.enabled_only
    )
    return ok_response(scheduled_tasks=[task.to_dict() for task in tasks])
  if args.command == "schedule-run-due":
    triggers = await ScheduledTaskService(uow_factory, _CliScheduledTaskLauncher(uow_factory)).run_due(limit=args.limit)
    return ok_response(triggers=[trigger.to_dict() for trigger in triggers])
  if args.command == "control-health":
    config = load_config_dict(args.config).get("control", {}) if args.config else {"browser": {"enabled": False}}
    control = ControlPlaneService.from_config(config if isinstance(config, dict) else {})
    if args.targets:
      return control.list_targets(args.kind)
    return await control.health()
  if args.command == "settle-memory":
    settlements = MemoryEvolutionSettlementService(uow_factory).settle_run(args.run_id, scope=args.scope)
    return ok_response(
      run_id=args.run_id,
      settlements=[
        {
          "candidate_id": item.candidate_id,
          "source_event_id": item.source_event_id,
          "memory_id": item.memory_id,
          "memory_type": item.memory_type,
          "scope": item.scope,
          "decision": item.decision,
        }
        for item in settlements
      ],
    )
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


def _build_delegation_broker(args: argparse.Namespace, uow_factory) -> AgentDelegationBroker:
  specs = load_product_cli_connector_specs(args.config)
  connectors = ProductCLIConnectorFactory().build_many(specs)
  return AgentDelegationBroker(uow_factory, connectors)


class _CliScheduledTaskLauncher:
  def __init__(self, uow_factory) -> None:
    self._launcher = None
    self._uow_factory = uow_factory

  async def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
    from agent_kernel.hosts.http import SampleWorkflowTaskLauncher

    if self._launcher is None:
      self._launcher = SampleWorkflowTaskLauncher(self._uow_factory)
    return await self._launcher.create_task(payload)


def _metadata_json(raw: str | None) -> dict[str, Any] | None:
  if raw is None:
    return None
  value = json.loads(raw)
  if not isinstance(value, dict):
    raise ValueError("--metadata-json must decode to a JSON object.")
  return value


if __name__ == "__main__":
  raise SystemExit(main())
