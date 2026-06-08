from pathlib import Path
import tempfile
import unittest

from agent_kernel.agents import (
  AgentInterventionChannel,
  CoordinatorProfile,
  FileTranscriptStore,
  LeaderPermissionBridge,
  TranscriptEntry,
  TranscriptResumeService,
  WorkerTaskNotification,
)
from agent_kernel.app.process_visibility import ProcessVisibilityProjector
from agent_kernel.capabilities.control_safety import (
  ComputerUseSafetyGate,
  ControlPermissionTier,
  ControlSafetyPolicy,
)
from agent_kernel.capabilities.protocol import CapabilityBatchPlanner, CapabilityProtocolDescriber
from agent_kernel.capabilities.adapters import (
  MCPAuthFailureCache,
  MCPConnectionBatchPolicy,
  MCPToolDescriptionLimiter,
  build_mcp_tool_name,
  is_mcp_session_expired_error,
)
from agent_kernel.context.reinjection import PostCompactStateReinjector, ReinjectableState
from agent_kernel.domain.capability import CapabilityConcurrency, CapabilityExecutionPolicy, CapabilitySpec, SideEffectLevel
from agent_kernel.domain.context import ModelContext
from agent_kernel.domain.events import RuntimeEvent, RuntimeEventType
from agent_kernel.models import MockModelProvider, ModelHealthRouter, ModelRoute, ModelGateway


class PlatformRuntimeMechanismTests(unittest.IsolatedAsyncioTestCase):
  def test_capability_protocol_describes_and_batches_tools(self) -> None:
    read = CapabilitySpec(
      capability_id="tool.read",
      name="Read",
      kind="tool",
      input_schema={},
      output_schema={},
      side_effect_level=SideEffectLevel.READ,
      execution_policy=CapabilityExecutionPolicy(read_only=True, concurrency=CapabilityConcurrency.SAFE),
    )
    write = CapabilitySpec(
      capability_id="tool.write",
      name="Write",
      kind="tool",
      input_schema={},
      output_schema={},
      side_effect_level=SideEffectLevel.WRITE,
    )

    profile = CapabilityProtocolDescriber().describe(read)
    batches = CapabilityBatchPlanner().plan([read, read, write, read])

    self.assertTrue(profile.can_run_concurrently)
    self.assertEqual([batch.kind for batch in batches], ["parallel", "sequential", "parallel"])
    self.assertEqual(batches[0].capability_ids, ["tool.read", "tool.read"])

  async def test_model_health_router_selects_healthier_fallback(self) -> None:
    primary = ModelRoute("primary", "main")
    fallback = ModelRoute("fallback", "main")
    router = ModelHealthRouter({primary.key: [fallback]})
    for _ in range(3):
      router.record_failure(primary, "empty_output")
    gateway = ModelGateway(router)
    primary_provider = MockModelProvider([{"content": "bad"}])
    fallback_provider = MockModelProvider([{"content": "ok"}])
    gateway.register_provider("primary", primary_provider)
    gateway.register_provider("fallback", fallback_provider)

    result = await gateway.complete("primary", "main", ModelContext(messages=[]))

    self.assertEqual(result["content"], "ok")
    self.assertEqual(len(primary_provider.calls), 0)
    self.assertEqual(len(fallback_provider.calls), 1)

  def test_post_compact_state_reinjector_preserves_active_work(self) -> None:
    state = ReinjectableState(
      objective="research",
      active_skill_ids=["browser_research"],
      active_workbench_ids=["workbench_1"],
      action_history=["opened search"],
    )
    injected = PostCompactStateReinjector().inject([{"role": "user", "content": "continue"}], state)

    self.assertEqual(injected[0]["role"], "system")
    self.assertEqual(injected[0]["content"]["active_skill_ids"], ["browser_research"])
    self.assertEqual(injected[1]["content"], "continue")

  def test_transcript_store_supports_main_sidechain_and_tail_metadata(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      store = FileTranscriptStore(Path(tmp))
      store.append(TranscriptEntry(entry_type="user", run_id="run_1", content={"text": "hello"}))
      store.append(
        TranscriptEntry(
          entry_type="assistant",
          run_id="run_1",
          agent_id="agent_a",
          sidechain=True,
          content={"text": "child"},
        )
      )
      store.append_metadata_tail("run_1", {"title": "Test"})

      main = TranscriptResumeService(store).load("run_1")
      child = TranscriptResumeService(store).load("run_1", "agent_a")

      self.assertEqual(main.metadata["title"], "Test")
      self.assertEqual(main.entries[0].content["text"], "hello")
      self.assertEqual(child.entries[0].content["text"], "child")

  def test_agent_intervention_and_permission_bridge_messages_are_structured(self) -> None:
    channel = AgentInterventionChannel()
    keyinfo = channel.keyinfo(recipient_session_id="child", sender_session_id="parent", content={"fact": "x"})
    bridge = LeaderPermissionBridge()
    request = bridge.request(
      parent_run_id="run",
      child_session_id="child",
      capability_id="tool.write",
      input_preview={"path": "a.txt"},
      reason="needs write",
    )
    response = bridge.response(request, approved=True)

    self.assertEqual(keyinfo.content["kind"], "keyinfo")
    self.assertEqual(request.to_parent_message()["capability_id"], "tool.write")
    self.assertTrue(response["approved"])

  def test_coordinator_profile_and_worker_notification_render_model_messages(self) -> None:
    profile = CoordinatorProfile(enabled=True, worker_budget=2).render_system_message()
    notification = WorkerTaskNotification(task_id="task_1", status="completed", summary="done").to_model_message()

    self.assertEqual(profile["content"]["role"], "orchestrator")
    self.assertEqual(notification["content"]["type"], "task_notification")
    self.assertEqual(notification["content"]["status"], "completed")

  def test_mcp_governance_limits_descriptions_and_auth_cache(self) -> None:
    limiter = MCPToolDescriptionLimiter(max_chars=8)
    cache = MCPAuthFailureCache()

    tool = limiter.normalize({"name": "search", "description": "x" * 20})
    cache.mark_failed("server", "401")

    self.assertEqual(build_mcp_tool_name("my server", "web search"), "mcp__my_server__web_search")
    self.assertTrue(tool["description_truncated"])
    self.assertTrue(cache.needs_auth("server"))
    self.assertEqual(MCPConnectionBatchPolicy().batch_size("stdio"), 3)
    self.assertTrue(is_mcp_session_expired_error(RuntimeError("HTTP 404 {\"code\":-32001}")))

  def test_computer_use_safety_gate_blocks_stale_input_and_dangerous_keys(self) -> None:
    gate = ComputerUseSafetyGate(ControlSafetyPolicy(app_tiers={"terminal": ControlPermissionTier.CLICK}))

    stale = gate.decide(action="click", target_kind="desktop", payload={}, context={"screenshot_fresh": False})
    key = gate.decide(action="key", target_kind="desktop", payload={"key": "Meta+Q"}, context={})
    tier = gate.decide(action="type_text", target_kind="desktop", payload={"text": "x"}, context={"app_id": "terminal"})

    self.assertFalse(stale.allowed)
    self.assertFalse(key.allowed)
    self.assertEqual(tier.type.value, "require_approval")

  def test_process_visibility_projects_runtime_events(self) -> None:
    events = [
      RuntimeEvent(event_type=RuntimeEventType.TOOL_CALL_STARTED, run_id="run", payload={"summary": "read"}),
      RuntimeEvent(event_type=RuntimeEventType.AGENT_DELEGATION_COMPLETED, run_id="run", payload={}),
      RuntimeEvent(event_type=RuntimeEventType.CONTEXT_BUILT, run_id="run", payload={}),
    ]

    activities = ProcessVisibilityProjector().from_events(events)

    self.assertEqual([item.kind for item in activities], ["tool_call", "agent_delegation", "context"])
    self.assertEqual(activities[0].status, "running")
    self.assertEqual(activities[1].status, "completed")


if __name__ == "__main__":
  unittest.main()
