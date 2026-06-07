"""Scenario verification for implemented TODO items.

This script intentionally validates only TODO items marked as implemented.
It uses temporary SQLite databases and optional real LLM config without printing secrets.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
  sys.path.insert(0, str(ROOT))

from agent_kernel.agents import (
  AgentLoop,
  AgentPoolScheduler,
  AgentSessionService,
  ConnectorMessage,
  ConnectorTurn,
  FakeAgentConnector,
  GroupChatService,
  InteractionFabric,
  ObserverService,
  SupervisorService,
  TaskBoardService,
  oneshot_agent_spec,
)
from agent_kernel.agents.mailbox import build_mailbox_message
from agent_kernel.autonomy import (
  ExplorationExecutor,
  ExplorationPlanner,
  ExplorationService,
  ExplorationVerifier,
  PlanPatchValidator,
  SkillService,
  SkillEvolutionService,
  TraceDistiller,
  WorkflowLibrary,
)
from agent_kernel.capabilities import CapabilityCallContext, CapabilityRegistry, CapabilityRuntime
from agent_kernel.capabilities.adapters import (
  FakeMCPClient,
  FakeWorkbenchClient,
  LocalToolExecutor,
  ProcessToolExecutor,
  WorkbenchCommand,
  WorkbenchResult,
)
from agent_kernel.config import LLMConfig
from agent_kernel.context.manager import ContextManager
from agent_kernel.domain import (
  AgentPool,
  AgentStatus,
  ArtifactRef,
  CapabilitySpec,
  CircuitBreakerState,
  CircuitStatus,
  EdgeSpec,
  ExecutionCommand,
  InteractionParticipant,
  NodeContext,
  NodeResult,
  NodeSpec,
  ParticipantKind,
  PlanPatch,
  RunState,
  RunStatus,
  RuntimeBudget,
  RuntimeEvent,
  RuntimeEventType,
  ToolResult,
  WorkflowSpec,
)
from agent_kernel.domain.agent import AgentTaskResult
from agent_kernel.domain.capability import SideEffectLevel
from agent_kernel.evaluation import ReplayService
from agent_kernel.evaluation.suites import ReplayEvalSuite
from agent_kernel.extensions import (
  ContributionRegistry,
  ExtensionManifestLoader,
  ExtensionPermissionMapper,
  ExtensionRuntime,
)
from agent_kernel.hosts.dto import EventStreamEnvelope, TaskWorkspaceDTO, default_http_routes
from agent_kernel.memory import (
  HTTPVectorStore,
  HTTPVectorStoreEndpoint,
  InMemoryVectorStore,
  MemoryFacade,
  VectorStoreSemanticRetriever,
)
from agent_kernel.models import MockModelProvider, ModelGateway, OpenAICompatibleProvider
from agent_kernel.observability import ArtifactInspectionService, CostService, MemoryAuditSink, TraceService
from agent_kernel.persistence import UnitOfWork, connect_sqlite
from agent_kernel.policy import (
  ApprovalService,
  HumanInterventionService,
  PolicyDecisionType,
  PolicyEngine,
)
from agent_kernel.runtime import RuntimeEngine, unit_of_work_factory
from agent_kernel.runtime.budget import BudgetUsage
from agent_kernel.runtime.engine import RuntimeOptions
from agent_kernel.runtime.retry import RetryClassifier
from agent_kernel.workflow import FunctionNodeExecutor, NodeExecutorRegistry, ToolNodeExecutor


Check = Callable[[], Awaitable[None]]


def _assert(condition: bool, message: str) -> None:
  if not condition:
    raise AssertionError(message)


def _workflow_three_nodes() -> WorkflowSpec:
  return WorkflowSpec(
    workflow_id="verify_wf_three",
    version="0.1.0",
    name="verify three nodes",
    input_schema={},
    output_schema={},
    nodes=[
      NodeSpec(node_id="start", kind="verify"),
      NodeSpec(node_id="middle", kind="verify"),
      NodeSpec(node_id="finish", kind="verify"),
    ],
    edges=[
      EdgeSpec(from_node="start", to_node="middle"),
      EdgeSpec(from_node="middle", to_node="finish"),
    ],
    start_node_id="start",
  )


def _runtime_engine(conn) -> RuntimeEngine:
  registry = NodeExecutorRegistry()

  def execute(ctx: NodeContext) -> NodeResult:
    if ctx.node.node_id == "finish":
      return NodeResult(state_patch={"finish": True}, command=ExecutionCommand(type="finish"))
    return NodeResult(state_patch={ctx.node.node_id: True})

  registry.register("verify", lambda: FunctionNodeExecutor(execute))
  return RuntimeEngine(unit_of_work_factory(conn), registry)


async def verify_phase0_domain_models() -> None:
  event = RuntimeEvent(event_type=RuntimeEventType.RUN_CREATED, run_id="run_domain")
  restored = RuntimeEvent.from_dict(event.to_dict())
  _assert(restored.event_type == RuntimeEventType.RUN_CREATED, "event serialization failed")

  state = RunState(run_id="run_domain", status=RunStatus.PENDING)
  _assert(RunState.from_dict(state.to_dict()).status == RunStatus.PENDING, "state serialization failed")


async def verify_phase1_runtime() -> None:
  conn = connect_sqlite()
  try:
    workflow = _workflow_three_nodes()
    engine = _runtime_engine(conn)
    created = engine.create_run(workflow, input={"request": "go"}, run_id="verify_run_runtime")
    completed = await engine.run_until_waiting(workflow, created.run_id)
    _assert(completed.status == RunStatus.COMPLETED, "runtime workflow did not complete")
    _assert(completed.variables["middle"] is True, "runtime reducer did not persist middle state")

    resumed_run = engine.create_run(workflow, input={}, run_id="verify_run_resume")
    after_one = await engine.run_until_waiting(workflow, resumed_run.run_id, max_steps=1)
    _assert(after_one.current_node_id == "middle", "runtime max_steps did not pause at middle")
    recreated = _runtime_engine(conn)
    resumed = await recreated.run_until_waiting(workflow, resumed_run.run_id)
    _assert(resumed.status == RunStatus.COMPLETED, "checkpoint resume did not complete")

    cancelled_run = engine.create_run(workflow, input={}, run_id="verify_run_cancel")
    running = await engine.run_until_waiting(workflow, cancelled_run.run_id, max_steps=1)
    cancelled = engine.cancel_run(running.run_id, "verify cancel")
    _assert(cancelled.status == RunStatus.CANCELLED, "run cancel did not persist cancelled state")

    calls = {"start": 0}
    registry = NodeExecutorRegistry()

    def flaky(ctx: NodeContext) -> NodeResult:
      if ctx.node.node_id == "start":
        calls["start"] += 1
        if calls["start"] == 1:
          raise TimeoutError("timeout calling tool")
      if ctx.node.node_id == "finish":
        return NodeResult(command=ExecutionCommand(type="finish"))
      return NodeResult(state_patch={ctx.node.node_id: True})

    registry.register("verify", lambda: FunctionNodeExecutor(flaky))
    retry_engine = RuntimeEngine(
      unit_of_work_factory(conn),
      registry,
      retry_classifier=RetryClassifier(max_attempts=2, base_delay_seconds=0),
    )
    retry_run = retry_engine.create_run(workflow, input={}, run_id="verify_run_retry")
    retry_completed = await retry_engine.run_until_waiting(workflow, retry_run.run_id)
    _assert(retry_completed.status == RunStatus.COMPLETED, "retry workflow did not complete")
    _assert(calls["start"] == 2, "retry did not re-execute transient step")

    fail_registry = NodeExecutorRegistry()
    fail_registry.register("verify", lambda: FunctionNodeExecutor(lambda ctx: (_ for _ in ()).throw(ValueError("bad"))))
    failed_run = RuntimeEngine(unit_of_work_factory(conn), fail_registry).create_run(
      workflow,
      input={},
      run_id="verify_run_dead_letter",
    )
    failed = await RuntimeEngine(unit_of_work_factory(conn), fail_registry).run_until_waiting(workflow, failed_run.run_id)
    _assert(failed.status == RunStatus.FAILED, "deterministic failure did not fail run")
    with UnitOfWork(conn) as uow:
      dead_letters = uow.dead_letters.list_all()
    _assert(dead_letters, "deterministic failure did not create dead letter")

    budget_run = engine.create_run(workflow, input={}, run_id="verify_run_budget")
    paused = await engine.run_until_waiting(
      workflow,
      budget_run.run_id,
      options=RuntimeOptions(
        budget=RuntimeBudget(
          budget_id="budget_verify",
          scope="run",
          scope_id="verify_run_budget",
          max_tool_calls=0,
          exhausted_action="pause",
        ),
        budget_usage=BudgetUsage(tool_calls=0),
      ),
    )
    _assert(paused.status == RunStatus.PAUSED, "budget exhaustion did not pause")

    circuit_run = engine.create_run(workflow, input={}, run_id="verify_run_circuit")
    circuit_paused = await engine.run_until_waiting(
      workflow,
      circuit_run.run_id,
      options=RuntimeOptions(
        circuit_state=CircuitBreakerState(
          circuit_id="circuit_verify",
          target_ref="verify",
          status=CircuitStatus.OPEN,
        )
      ),
    )
    _assert(circuit_paused.status == RunStatus.PAUSED, "open circuit did not pause")
  finally:
    conn.close()


async def verify_phase2_agent(real_config: LLMConfig | None) -> None:
  conn = connect_sqlite()
  try:
    uow_factory = unit_of_work_factory(conn)
    sessions = AgentSessionService(uow_factory)
    supervisor = SupervisorService(uow_factory, sessions)
    parent = sessions.create_session(oneshot_agent_spec("verify_parent", "mock"))
    child = supervisor.spawn_child(
      parent.session_id,
      oneshot_agent_spec("verify_child", "mock"),
      {"task_id": "task_verify", "title": "finish child"},
    )
    gateway = ModelGateway()
    gateway.register_provider("mock", MockModelProvider([{"finish": True, "output": {"summary": "child done"}}]))
    turn = await AgentLoop(uow_factory, gateway).run_once(child.session_id, "mock")
    _assert(turn.session.status == AgentStatus.COMPLETED, "child agent did not complete")
    _assert(supervisor.get_child_result(child.session_id).output["summary"] == "child done", "child result not returned")

    cancel_parent = sessions.create_session(oneshot_agent_spec("verify_cancel_parent", "mock"))
    child_a = supervisor.spawn_child(cancel_parent.session_id, oneshot_agent_spec("verify_child_a", "mock"), {})
    child_b = supervisor.spawn_child(cancel_parent.session_id, oneshot_agent_spec("verify_child_b", "mock"), {})
    cancelled = supervisor.cancel_children(cancel_parent.session_id)
    _assert(len(cancelled) >= 2, "cascade cancel did not return child sessions")
    _assert(child_a.session_id and child_b.session_id, "child lineage missing")

    if real_config is not None:
      real_gateway = ModelGateway()
      real_gateway.register_provider(
        real_config.provider,
        OpenAICompatibleProvider(
          api_key=real_config.api_key,
          base_url=real_config.base_url,
          timeout_seconds=real_config.timeout_seconds,
        ),
      )
      real_session = sessions.create_session(oneshot_agent_spec("verify_real_agent", real_config.model))
      with uow_factory() as uow:
        uow.mailbox.send(
          build_mailbox_message(
            recipient_session_id=real_session.session_id,
            mailbox_id=real_session.mailbox_id,
            content={
              "instruction": (
                "只返回严格 JSON: "
                '{"finish": true, "output": {"summary": "real agent ok"}}'
              )
            },
          )
        )
      real_turn = await AgentLoop(uow_factory, real_gateway, provider_name=real_config.provider).run_once(
        real_session.session_id,
        real_config.model,
      )
      _assert(real_turn.session.status == AgentStatus.COMPLETED, "real LLM AgentLoop did not complete")
      _assert(isinstance(real_turn.result, AgentTaskResult), "real LLM AgentLoop did not persist result")
  finally:
    conn.close()


async def verify_phase3_capability_policy() -> None:
  conn = connect_sqlite()
  try:
    uow_factory = unit_of_work_factory(conn)
    registry = CapabilityRegistry()
    registry.register(
      CapabilitySpec(
        capability_id="tool.echo",
        name="echo",
        kind="tool",
        input_schema={},
        output_schema={},
        side_effect_level=SideEffectLevel.NONE,
      )
    )
    local_tools = LocalToolExecutor()
    local_tools.register("tool.echo", lambda input: ToolResult(ok=True, output={"echo": input["text"]}))
    audit_sink = MemoryAuditSink()
    runtime = CapabilityRuntime(registry, PolicyEngine(), local_tools, audit_sink=audit_sink, uow_factory=uow_factory)
    outcome = await runtime.call("tool.echo", {"text": "ok"}, ctx=_call_ctx("verify_capability"))
    _assert(outcome.result is not None and outcome.result.output["echo"] == "ok", "local capability failed")

    registry.register(
      CapabilitySpec(
        capability_id="tool.exec",
        name="exec",
        kind="tool",
        input_schema={},
        output_schema={},
        side_effect_level=SideEffectLevel.EXEC,
      )
    )
    high_risk = await runtime.call("tool.exec", {}, ctx=_call_ctx("verify_approval"))
    _assert(high_risk.requires_approval, "high risk capability did not require approval")

    approval_service = ApprovalService(uow_factory)
    request = approval_service.request_tool_approval("verify_approval", "tool.exec", "verify", {})
    grant = approval_service.approve(request.approval_id)
    _assert(grant.capability_id == "tool.exec", "approval did not create grant")
    rejected = approval_service.reject(
      approval_service.request_tool_approval("verify_approval", "tool.write", "verify", {}).approval_id
    )
    _assert(rejected.status == "rejected", "approval reject did not persist")

    process_tools = ProcessToolExecutor()
    process_tools.register("proc.echo", [sys.executable, "-c", "print('process-ok')"], timeout_seconds=5)
    registry.register(
      CapabilitySpec(
        capability_id="proc.echo",
        name="proc echo",
        kind="tool",
        input_schema={},
        output_schema={},
        side_effect_level=SideEffectLevel.NONE,
      )
    )
    process_runtime = CapabilityRuntime(
      registry,
      PolicyEngine(),
      LocalToolExecutor(),
      process_tools=process_tools,
      uow_factory=uow_factory,
    )
    process_outcome = await process_runtime.call("proc.echo", {}, ctx=_call_ctx("verify_process"))
    _assert("process-ok" in process_outcome.result.output["stdout"], "process tool did not execute")
    process_tools.register(
      "proc.structured",
      [
        sys.executable,
        "-u",
        "-c",
        "import json; print(json.dumps(dict(event='progress', payload=dict(step=1))), flush=True)",
      ],
      timeout_seconds=5,
    )
    stream_events = [event async for event in process_tools.stream_structured("proc.structured", {})]
    _assert(any(event.event == "structured" and event.name == "progress" for event in stream_events), "structured process stream failed")

    mcp = FakeMCPClient()
    mcp.register_response("server", "tool", ToolResult(ok=True, output={"mcp": "ok"}))
    mcp_result = await mcp.call_tool("server", "tool", {"arg": 1})
    _assert(mcp_result.output["mcp"] == "ok", "fake MCP adapter failed")

    workbench = FakeWorkbenchClient()
    workbench.register_response("inspect", WorkbenchResult(ok=True, output={"status": "ok"}))
    wb_result = await workbench.execute(WorkbenchCommand(command_id="cmd_1", kind="inspect"))
    _assert(wb_result.output["status"] == "ok", "fake workbench adapter failed")

    with UnitOfWork(conn) as uow:
      tool_calls = uow.tool_calls.list_by_run("verify_process")
      audit = uow.audit.list_by_run("verify_capability")
    _assert(tool_calls[0].status == "succeeded", "tool call status not persisted")
    _assert(audit, "audit not persisted")
    sink_decisions = [record.decision for record in audit_sink.records]
    _assert(sink_decisions[:2] == ["allow", "result"], "audit sink did not emit success records")
    _assert("require_approval" in sink_decisions, "audit sink did not emit approval record")
  finally:
    conn.close()


def _call_ctx(run_id: str):
  from agent_kernel.capabilities.runtime import CapabilityCallContext

  return CapabilityCallContext(run_id=run_id, agent_id="agent_verify")


async def verify_phase4_memory_context() -> None:
  conn = connect_sqlite()
  try:
    uow_factory = unit_of_work_factory(conn)
    memory = MemoryFacade(uow_factory)
    memory.write_working("scope_verify", {"fact": "important"}, importance=0.9)
    memory.write_working("scope_verify", {"secret": "hidden"}, importance=1.0)
    large = memory.write_working("scope_verify", {"large": "x" * 2000}, importance=0.8)
    _assert(large.source_artifact_refs, "large memory did not create artifact ref")

    context = ContextManager(uow_factory, memory, max_tokens=512).build(
      run_id="verify_context",
      scope="scope_verify",
      model_ref="mock",
      messages=[{"role": "user", "content": "use memory"}],
      available_tools=[{"name": "safe"}, {"name": "unsafe"}],
      tool_allowlist={"safe"},
    )
    _assert(context.memory_refs, "context did not include memory refs")
    _assert(context.tool_schemas == [{"name": "safe"}], "tool visibility pruning failed")
    _assert(context.inclusion_rationale, "context rationale missing")
    with UnitOfWork(conn) as uow:
      events = uow.events.list_by_run("verify_context")
    _assert(any(event.event_type == RuntimeEventType.CONTEXT_BUILT for event in events), "context ledger missing")

    vector_memory = MemoryFacade(
      uow_factory,
      semantic_retriever=VectorStoreSemanticRetriever(InMemoryVectorStore()),
    )
    vector_memory.write_semantic(
      "scope_vector",
      {"summary": "Runtime checkpoint replay uses sqlite events", "keywords": ["runtime", "checkpoint", "sqlite"]},
      importance=0.9,
    )
    vector_memory.write_semantic("scope_vector", {"summary": "Unrelated visual polish"}, importance=1.0)
    vector_results = vector_memory.retrieve_semantic("scope_vector", "sqlite checkpoint replay", limit=1)
    _assert(vector_results and "vector store" in vector_results[0].rationale, "vector store retrieval failed")

    http_transport = _VerifyHTTPVectorTransport()
    http_memory = MemoryFacade(
      uow_factory,
      semantic_retriever=VectorStoreSemanticRetriever(
        HTTPVectorStore(
          HTTPVectorStoreEndpoint(base_url="https://vector.verify"),
          transport=http_transport.post,
        )
      ),
    )
    http_item = http_memory.write_semantic("scope_http_vector", {"summary": "HTTP vector checkpoint memory"})
    http_transport.match_id = http_item.memory_id
    http_results = http_memory.retrieve_semantic("scope_http_vector", "checkpoint", limit=1)
    _assert(http_results and http_results[0].memory.memory_id == http_item.memory_id, "HTTP vector retrieval failed")
    _assert(http_transport.urls == ["https://vector.verify/upsert", "https://vector.verify/query"], "HTTP vector routes wrong")
  finally:
    conn.close()


class _VerifyHTTPVectorTransport:
  def __init__(self) -> None:
    self.urls: list[str] = []
    self.match_id = ""

  def post(self, url, payload, endpoint):
    self.urls.append(url)
    if url.endswith("/query"):
      return {"matches": [{"document_id": self.match_id, "score": 0.8}]}
    return {"ok": True}


async def verify_phase5_observability_replay() -> None:
  conn = connect_sqlite()
  try:
    workflow = _workflow_three_nodes()
    engine = _runtime_engine(conn)
    run = engine.create_run(workflow, input={}, run_id="verify_replay")
    completed = await engine.run_until_waiting(workflow, run.run_id)
    _assert(completed.status == RunStatus.COMPLETED, "replay fixture workflow did not complete")

    uow_factory = unit_of_work_factory(conn)
    timeline = TraceService(uow_factory).build_timeline("verify_replay")
    replay = ReplayService(uow_factory).exact_replay("verify_replay")
    partial = ReplayService(uow_factory).partial_replay("verify_replay", timeline.entries[0].ref_id)
    recovery = ReplayService(uow_factory).recovery_replay("verify_replay")
    _assert(timeline.entries, "timeline missing entries")
    _assert(replay.final_state.status == RunStatus.COMPLETED, "exact replay final state wrong")
    _assert(partial.replayed_external_calls == 0, "partial replay external calls not zero")
    _assert(recovery.replayed_external_calls == 0, "recovery replay external calls not zero")

    with UnitOfWork(conn) as uow:
      uow.events.append(
        RuntimeEvent(
          event_type=RuntimeEventType.MODEL_CALL_COMPLETED,
          run_id="verify_replay",
          payload={"usage": {"input_tokens": 1, "output_tokens": 2, "cost_usd": 0.3}},
        )
      )
      art = ArtifactRef(artifact_id="art_verify", uri="artifact://verify")
      uow.artifacts.save(art)
      uow.states.save(
        RunState(run_id="verify_artifact", status=RunStatus.COMPLETED, artifact_refs=[art])
      )
    cost = CostService(uow_factory).build_ledger("verify_replay")
    _assert(cost.model_calls == 1 and cost.cost_usd == 0.3, "cost ledger failed")
    inspection = ArtifactInspectionService(uow_factory).inspect_run("verify_artifact")
    _assert(inspection.artifact_refs[0].artifact_id == "art_verify", "artifact inspect failed")

    suite_result = ReplayEvalSuite().evaluate_completed_run(replay)
    _assert(suite_result.passed, "replay eval suite failed")
  finally:
    conn.close()


async def verify_phase6_extensions() -> None:
  manifest_data = {
    "extension_id": "verify.ext",
    "name": "Verify Extension",
    "version": "0.1.0",
    "compatible_kernel": ">=0.1.0",
    "side_effect_level": "write",
    "permissions": ["capability:verify.ext.write"],
    "contributes": [
      {
        "kind": "tool_provider",
        "name": "write",
        "entrypoint": "verify:write",
        "config_schema": {"required_grant": "fs.write"},
      }
    ],
  }
  manifest = ExtensionManifestLoader().load_dict(manifest_data)
  contributions = ContributionRegistry()
  capabilities = CapabilityRegistry()
  contributions.register_manifest(manifest)
  contributions.register_tool_capabilities(manifest, capabilities)
  spec = capabilities.get("verify.ext.write")
  grants = ExtensionPermissionMapper().grants_for_manifest(manifest, run_id="verify_extension")
  decision = PolicyEngine(grants=grants).decide(spec, run_id="verify_extension")
  _assert(decision.type == PolicyDecisionType.ALLOW, "extension permission did not map to allow grant")

  with tempfile.TemporaryDirectory() as tmp:
    module_path = Path(tmp) / "verify_dynamic_ext.py"
    module_path.write_text(
      "\n".join(
        [
          "def provide_echo(context):",
          "    def echo(payload):",
          "        return {'echo': payload['text'], 'capability_id': context.capability_id}",
          "    return echo",
        ]
      ),
      encoding="utf-8",
    )
    sys.path.insert(0, tmp)
    try:
      dynamic_manifest = ExtensionManifestLoader().load_dict(
        {
          "extension_id": "verify.dynamic",
          "name": "Verify Dynamic Extension",
          "version": "0.1.0",
          "compatible_kernel": ">=0.1.0",
          "side_effect_level": "none",
          "contributes": [
            {
              "kind": "tool_provider",
              "name": "echo",
              "entrypoint": "verify_dynamic_ext:provide_echo",
              "config_schema": {
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
              },
            }
          ],
        }
      )
      dynamic_capabilities = CapabilityRegistry()
      local_tools = LocalToolExecutor()
      ExtensionRuntime().load_manifest(
        dynamic_manifest,
        capability_registry=dynamic_capabilities,
        local_tools=local_tools,
        contribution_registry=ContributionRegistry(),
      )
      runtime = CapabilityRuntime(dynamic_capabilities, PolicyEngine(), local_tools)
      outcome = await runtime.call(
        "verify.dynamic.echo",
        {"text": "dynamic-ok"},
        CapabilityCallContext(run_id="verify_dynamic_extension"),
      )
    finally:
      sys.path.remove(tmp)
      sys.modules.pop("verify_dynamic_ext", None)
  _assert(outcome.result is not None and outcome.result.output["echo"] == "dynamic-ok", "dynamic extension tool failed")
  _assert(outcome.result.output["capability_id"] == "verify.dynamic.echo", "dynamic extension context failed")


async def verify_phase7_autonomy() -> None:
  conn = connect_sqlite()
  try:
    uow_factory = unit_of_work_factory(conn)
    executor = ExplorationExecutor(
      uow_factory,
      attempt_executor=lambda strategy: {"ok": True, "summary": "success", "event_refs": ["evt_verify"]},
      verifier=ExplorationVerifier(),
    )
    service = ExplorationService(
      uow_factory,
      planner=ExplorationPlanner(),
      executor=executor,
      distiller=TraceDistiller(),
      workflow_library=WorkflowLibrary(uow_factory),
    )
    task = service.create_task("verify_objective", "solve open task", acceptance_criteria=["works"])
    template = service.run(task)
    _assert(template is not None and template.status == "draft", "autonomy did not publish draft template")
    with UnitOfWork(conn) as uow:
      attempt = uow.autonomy.list_attempts(task.exploration_id)[0]
    trace = TraceDistiller().distill(attempt)
    record = SkillEvolutionService(uow_factory).record_compiled_workflow(trace, template, "verified")
    _assert(record.workflow_template_id == template.template_id, "skill evolution record failed")

    skill_service = SkillService(uow_factory)
    skill = skill_service.create_interpreted_skill(
      name="api-review",
      description="Review API contracts",
      when_to_use="api design",
      instructions="Check schemas and edge cases.",
    )
    skill_service.activate(skill.skill_id)
    _assert(skill_service.select_for_prompt("api design"), "skill selection failed")

    capabilities = CapabilityRegistry()
    capabilities.register(
      CapabilitySpec(
        capability_id="tool.safe",
        name="safe",
        kind="tool",
        input_schema={},
        output_schema={},
        side_effect_level=SideEffectLevel.NONE,
      )
    )
    patch = PlanPatch(
      patch_id="patch_verify",
      run_id="run_verify",
      proposed_by_agent_id="agent_verify",
      reason="go next",
      commands=[ExecutionCommand(type="goto", target="finish")],
      required_capabilities=["tool.safe"],
    )
    validation = PlanPatchValidator(capabilities).validate(patch, _workflow_three_nodes())
    _assert(validation.ok, f"plan patch validation failed: {validation.errors}")
  finally:
    conn.close()


async def verify_phase8_interaction() -> None:
  conn = connect_sqlite()
  try:
    uow_factory = unit_of_work_factory(conn)
    fabric = InteractionFabric(uow_factory)
    backend = fabric.add_participant(
      InteractionParticipant(
        participant_id="p_backend",
        kind=ParticipantKind.AGENT,
        role="backend",
        agent_session_id="session_backend",
      )
    )
    frontend = fabric.add_participant(
      InteractionParticipant(
        participant_id="p_frontend",
        kind=ParticipantKind.AGENT,
        role="frontend",
        agent_session_id="session_frontend",
      )
    )
    channel = fabric.create_channel("API design", [backend.participant_id, frontend.participant_id])
    fabric.send_message(channel.channel_id, backend.participant_id, {"text": "define API"})
    _assert(fabric.list_messages(channel.channel_id)[0].content["text"] == "define API", "channel message failed")

    chat = GroupChatService(uow_factory, fabric).create(
      "thread_verify",
      "topic",
      [backend.participant_id, frontend.participant_id],
    )
    turn1 = GroupChatService(uow_factory, fabric).add_turn(chat, channel.channel_id, {"text": "first"})
    turn2 = GroupChatService(uow_factory, fabric).add_turn(chat, channel.channel_id, {"text": "second"})
    _assert(turn1.speaker_participant_id == backend.participant_id, "round robin first speaker wrong")
    _assert(turn2.speaker_participant_id == frontend.participant_id, "round robin second speaker wrong")

    pool = AgentPool(
      pool_id="pool_verify",
      name="backend pool",
      role="backend",
      agent_session_ids=["session_backend", "session_backend_2"],
    )
    with UnitOfWork(conn) as uow:
      uow.interactions.save_agent_pool(pool)
    selected = AgentPoolScheduler().select(pool)
    item = TaskBoardService(uow_factory).create_item("implement API", assignee_pool_id=pool.pool_id)
    assigned = TaskBoardService(uow_factory).assign(item, selected)
    finding = ObserverService(uow_factory).request_pause("observer", "run_observed", "stop")
    _assert(assigned.status == "doing", "taskboard assign failed")
    _assert(finding.action == "request_pause", "observer finding failed")

    connector = FakeAgentConnector()
    connector.queue_response(
      ConnectorTurn(
        turn_id="turn_1",
        session_id="external_session",
        output={"summary": "connector ok"},
        completed=True,
      )
    )
    await connector.start("external_session")
    turn = await connector.send(
      ConnectorMessage(
        message_id="msg_connector",
        session_id="external_session",
        content={"task": "coordinate"},
      )
    )
    await connector.stop("external_session", "done")
    _assert(turn.completed and turn.output["summary"] == "connector ok", "fake agent connector failed")
  finally:
    conn.close()


async def verify_phase9_cli_and_llm(config_path: Path | None, real_config: LLMConfig | None) -> None:
  from agent_kernel.hosts.cli import _dispatch, build_parser

  db = Path(tempfile.gettempdir()) / "agent_kernel_todo_verify_cli.sqlite"
  conn = connect_sqlite(db)
  try:
    parser = build_parser()
    payload = await _dispatch(
      parser.parse_args(["--db", str(db), "sample-run", "--run-id", "verify_cli", "--text", "hello"]),
      conn,
    )
  finally:
    conn.close()
  _assert(payload["status"] == "completed", "CLI sample-run did not complete")

  conn = connect_sqlite(db)
  try:
    parser = build_parser()
    inspect_payload = await _dispatch(parser.parse_args(["--db", str(db), "inspect", "verify_cli"]), conn)
  finally:
    conn.close()
  _assert(inspect_payload["entries"], "CLI inspect failed")

  conn = connect_sqlite(db)
  try:
    parser = build_parser()
    replay_payload = await _dispatch(parser.parse_args(["--db", str(db), "replay", "verify_cli"]), conn)
  finally:
    conn.close()
  _assert(replay_payload["status"] == "completed", "CLI replay failed")

  conn = connect_sqlite(db)
  try:
    with unit_of_work_factory(conn)() as uow:
      uow.states.save(RunState(run_id="verify_cli_intervene", status=RunStatus.RUNNING))
    intervention = HumanInterventionService(unit_of_work_factory(conn)).apply(
      run_id="verify_cli_intervene",
      content="correct course",
      apply_mode="pause_and_resume",
    )
  finally:
    conn.close()
  _assert(intervention.run_status == RunStatus.INTERRUPTED, "human intervention did not interrupt run")

  envelope = EventStreamEnvelope(
    event_id="evt_verify",
    event_type="run.created",
    run_id="verify_cli",
    payload={"ok": True},
  )
  workspace = TaskWorkspaceDTO(workspace_id="workspace_verify", title="Verify", run_ids=["verify_cli"])
  _assert(envelope.to_dict()["event_type"] == "run.created", "event stream DTO failed")
  _assert(workspace.to_dict()["run_ids"] == ["verify_cli"], "workspace DTO failed")
  _assert(default_http_routes(), "default HTTP route specs missing")

  if config_path is not None and real_config is not None:
    conn = connect_sqlite(db)
    try:
      parser = build_parser()
      llm_payload = await _dispatch(
        parser.parse_args(
          [
            "--config",
            str(config_path),
            "llm-smoke",
            "--prompt",
            "请只回复：real cli llm ok",
          ]
        ),
        conn,
      )
    finally:
      conn.close()
    _assert(llm_payload["ok"], "CLI real llm-smoke failed")
    _assert(llm_payload["model"] == real_config.model, "CLI real llm-smoke used wrong model")


async def run_checks(config_path: Path | None, include_real_llm: bool) -> None:
  real_config = LLMConfig.load(config_path) if include_real_llm else None
  checks: list[tuple[str, Check]] = [
    ("Phase 0 domain models", verify_phase0_domain_models),
    ("Phase 1 durable runtime", verify_phase1_runtime),
    ("Phase 2 agent orchestration", lambda: verify_phase2_agent(real_config)),
    ("Phase 3 capability/policy", verify_phase3_capability_policy),
    ("Phase 4 memory/context", verify_phase4_memory_context),
    ("Phase 5 observability/replay", verify_phase5_observability_replay),
    ("Phase 6 extension SDK", verify_phase6_extensions),
    ("Phase 7 autonomy/skill evolution", verify_phase7_autonomy),
    ("Phase 8 interaction fabric", verify_phase8_interaction),
    ("Phase 9 CLI/LLM host", lambda: verify_phase9_cli_and_llm(config_path, real_config)),
  ]

  for label, check in checks:
    await check()
    print(f"PASS {label}")


def main() -> int:
  parser = argparse.ArgumentParser()
  parser.add_argument("--config", default="agent-kernel.toml")
  parser.add_argument("--no-real-llm", action="store_true")
  args = parser.parse_args()

  config_path = Path(args.config) if args.config else None
  if args.no_real_llm:
    config_path = None
  asyncio.run(run_checks(config_path=config_path, include_real_llm=not args.no_real_llm))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
