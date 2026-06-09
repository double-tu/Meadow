import unittest

from agent_kernel.agents import ContinuousAgentRunner, ContinuousRunnerConfig
from datetime import timedelta

from agent_kernel.autonomy import SkillService
from agent_kernel.autonomy.builtin_skills import ensure_builtin_atomic_skills
from agent_kernel.capabilities import AtomicCapabilityIds, AtomicCapabilityProvider, CapabilityRegistry, CapabilityRuntime
from agent_kernel.capabilities.adapters import LocalToolExecutor
from agent_kernel.context import ContextAssembler
from agent_kernel.domain import ArtifactRef, CapabilityGrant, RuntimeEventType, SkillCard
from agent_kernel.domain.base import utc_now
from agent_kernel.memory import MemoryFacade
from agent_kernel.models import MockModelProvider, ModelGateway
from agent_kernel.persistence import UnitOfWork, connect_sqlite
from agent_kernel.policy import PolicyEngine
from agent_kernel.runtime import unit_of_work_factory


class ContextAssemblerTests(unittest.TestCase):
  def test_assembler_builds_seven_layers_and_compact_skill_index(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      memory = MemoryFacade(uow_factory)
      working = memory.write_working("chat_1", {"active_goal": "inspect browser"}, importance=0.9)
      semantic = memory.write_semantic(
        "chat_1",
        {"summary": "User prefers concise Chinese replies", "keywords": ["preference", "Chinese"]},
        importance=0.8,
      )
      artifact_ref = ArtifactRef(
        artifact_id="art_page_summary",
        uri="artifact://page-summary",
        media_type="application/json",
      )
      artifact_memory = memory.write_artifact_memory(
        "chat_1",
        artifact_ref,
        {"summary": "Browser page summary is stored outside prompt."},
        importance=0.7,
      )
      skill = SkillCard(
        skill_id="skill_browser",
        name="Browser research",
        description="Use browser tools to inspect pages.",
        when_to_use="When the user asks to browse or inspect a page.",
        instructions="SECRET_FULL_BROWSER_PROCEDURE_SHOULD_NOT_BE_IN_INDEX",
        recommended_tools=["browser_scan", "browser_navigate"],
      )
      assembler = ContextAssembler(uow_factory=uow_factory, memory=memory)

      pack = assembler.assemble(
        request=_request(
          run_id="run_ctx_assembler",
          scope="chat_1",
          skills=[skill],
          tool_schemas=[
            {
              "name": "browser_scan",
              "description": "Inspect current browser pages.",
              "capability_id": "atom.browser.scan",
            }
          ],
        )
      )

      self.assertEqual([layer.kind.value for layer in pack.layers], [
        "system_policy",
        "system_policy",
        "agent_profile",
        "skill_tool_index",
        "working_memory",
        "conversation_window",
        "episodic_artifact",
        "long_term_memory",
      ])
      rendered = str(pack.model_context.messages)
      self.assertEqual([message["role"] for message in pack.model_context.messages].count("system"), 1)
      self.assertIn("Meadow Operating Context", rendered)
      self.assertIn("Capability Navigation", rendered)
      self.assertIn("builtin.atomic.web_research", rendered)
      self.assertIn("No execution, no memory", rendered)
      self.assertIn("skill_browser", rendered)
      self.assertIn("Skill index is compact", rendered)
      self.assertNotIn("SECRET_FULL_BROWSER_PROCEDURE", rendered)
      self.assertIn(working.memory_id, [ref.memory_id for ref in pack.model_context.memory_refs])
      self.assertIn(semantic.memory_id, [ref.memory_id for ref in pack.model_context.memory_refs])
      self.assertIn(artifact_memory.memory_id, [ref.memory_id for ref in pack.model_context.memory_refs])
      self.assertEqual(pack.model_context.attachments[0].artifact_id, artifact_ref.artifact_id)

      with UnitOfWork(conn) as uow:
        events = uow.events.list_by_run("run_ctx_assembler")
      self.assertEqual(events[0].event_type, RuntimeEventType.CONTEXT_BUILT)
      self.assertEqual(events[0].payload["context_pack_id"], pack.pack_id)
    finally:
      conn.close()

  def test_small_budget_keeps_critical_layer_items_and_omits_low_priority_context(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      assembler = ContextAssembler(uow_factory=uow_factory, memory=MemoryFacade(uow_factory))

      pack = assembler.assemble(
        request=_request(
          run_id="run_ctx_budget",
          scope="chat_budget",
          max_tokens=80,
          messages=[
            {"role": "user", "content": "old " + "x" * 1000},
            {"role": "user", "content": "new question"},
          ],
        )
      )

      self.assertTrue(pack.model_context.messages)
      self.assertIn("new question", str(pack.model_context.messages))
      self.assertIn("context_layer_budget_omitted_items", pack.quality_warnings)
    finally:
      conn.close()

  def test_builtin_sop_skill_index_includes_compact_procedure_hint(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      skill_service = SkillService(uow_factory)
      skills = ensure_builtin_atomic_skills(skill_service)
      browser_skill = next(skill for skill in skills if skill.skill_id == "builtin.atomic.web_research")
      assembler = ContextAssembler(uow_factory=uow_factory, memory=MemoryFacade(uow_factory))

      pack = assembler.assemble(
        request=_request(
          run_id="run_ctx_builtin_sop_hint",
          scope="chat_builtin_sop_hint",
          skills=[browser_skill],
        )
      )

      rendered = str(pack.model_context.messages)
      self.assertIn("hint:", rendered)
      self.assertIn("动态推荐", rendered)
      self.assertIn("title/text/url/author/time/metrics", rendered)
      self.assertIn("resource_refs:", rendered)
      self.assertIn("builtin.resource.browser_research_sop", rendered)
      self.assertNotIn("Treat search result pages as candidate discovery only", rendered)
    finally:
      conn.close()

  def test_custom_skill_can_expose_editable_index_hint_without_full_instructions(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      skill = SkillCard(
        skill_id="custom.web_feed",
        name="Custom feed reader",
        description="Read feed cards.",
        when_to_use="When the user asks for feed cards.",
        instructions=(
          "INDEX_HINT: use browser_execute_js to extract visible feed cards.\n"
          "SECRET_FULL_FEED_PROCEDURE_SHOULD_NOT_BE_IN_INDEX"
        ),
        recommended_tools=["browser_execute_js"],
      )
      assembler = ContextAssembler(uow_factory=uow_factory, memory=MemoryFacade(uow_factory))

      pack = assembler.assemble(
        request=_request(
          run_id="run_ctx_custom_index_hint",
          scope="chat_custom_index_hint",
          skills=[skill],
        )
      )

      rendered = str(pack.model_context.messages)
      self.assertIn("use browser_execute_js to extract visible feed cards", rendered)
      self.assertNotIn("SECRET_FULL_FEED_PROCEDURE", rendered)
    finally:
      conn.close()

  def test_conversation_window_preserves_tool_transcript_fields(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      assembler = ContextAssembler(uow_factory=uow_factory, memory=MemoryFacade(uow_factory))

      pack = assembler.assemble(
        request=_request(
          run_id="run_ctx_tool_transcript",
          scope="chat_tool_transcript",
          messages=[
            {"role": "user", "content": "read file"},
            {
              "role": "assistant",
              "content": "",
              "tool_calls": [
                {
                  "id": "call_1",
                  "type": "function",
                  "function": {"name": "workspace_read", "arguments": "{\"path\":\"a.txt\"}"},
                }
              ],
            },
            {"role": "tool", "tool_call_id": "call_1", "name": "workspace_read", "content": "{\"ok\":true}"},
          ],
        )
      )

      assistant_messages = [message for message in pack.model_context.messages if message.get("role") == "assistant"]
      tool_messages = [message for message in pack.model_context.messages if message.get("role") == "tool"]
      self.assertEqual(assistant_messages[-1]["tool_calls"][0]["id"], "call_1")
      self.assertEqual(tool_messages[-1]["tool_call_id"], "call_1")
    finally:
      conn.close()

  def test_conversation_window_does_not_orphan_selected_tool_calls_when_trimmed(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      assembler = ContextAssembler(uow_factory=uow_factory, memory=MemoryFacade(uow_factory))

      pack = assembler.assemble(
        request=_request(
          run_id="run_ctx_trimmed_tool_transcript",
          scope="chat_trimmed_tool_transcript",
          max_tokens=900,
          messages=[
            {"role": "user", "content": "read file"},
            {
              "role": "assistant",
              "content": None,
              "tool_calls": [
                {
                  "id": "call_1",
                  "type": "function",
                  "function": {"name": "workspace_read", "arguments": "{\"path\":\"a.txt\"}"},
                }
              ],
            },
            {"role": "tool", "tool_call_id": "call_1", "name": "workspace_read", "content": "{\"ok\":true}"},
            {
              "role": "user",
              "content": {
                "type": "runtime_context",
                "instruction": "x" * 1800,
              },
            },
          ],
        )
      )

      messages = pack.model_context.messages
      assistant_index = next(
        index for index, message in enumerate(messages) if message.get("role") == "assistant" and message.get("tool_calls")
      )
      self.assertEqual(messages[assistant_index + 1]["role"], "tool")
      self.assertEqual(messages[assistant_index + 1]["tool_call_id"], "call_1")
    finally:
      conn.close()

  def test_conversation_window_repairs_partial_multi_tool_call_groups(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      assembler = ContextAssembler(uow_factory=uow_factory, memory=MemoryFacade(uow_factory))

      pack = assembler.assemble(
        request=_request(
          run_id="run_ctx_trimmed_multi_tool_transcript",
          scope="chat_trimmed_multi_tool_transcript",
          max_tokens=300,
          messages=[
            {"role": "user", "content": "deep search"},
            {
              "role": "assistant",
              "content": None,
              "tool_calls": [
                {
                  "id": "call_search",
                  "type": "function",
                  "function": {"name": "browser_search", "arguments": "{\"q\":\"pricing\"}"},
                },
                {
                  "id": "call_open",
                  "type": "function",
                  "function": {"name": "browser_open", "arguments": "{\"url\":\"https://example.com\"}"},
                },
              ],
            },
            {
              "role": "tool",
              "tool_call_id": "call_search",
              "name": "browser_search",
              "content": "{\"results\":[" + ("\"" + "x" * 500 + "\",") * 10 + "\"done\"]}",
            },
            {
              "role": "tool",
              "tool_call_id": "call_open",
              "name": "browser_open",
              "content": "{\"title\":\"Pricing\"}",
            },
          ],
        )
      )

      messages = pack.model_context.messages
      assistant_index = next(
        index for index, message in enumerate(messages) if message.get("role") == "assistant" and message.get("tool_calls")
      )
      self.assertEqual([call["id"] for call in messages[assistant_index]["tool_calls"]], ["call_search", "call_open"])
      self.assertEqual(messages[assistant_index + 1]["role"], "tool")
      self.assertEqual(messages[assistant_index + 1]["tool_call_id"], "call_search")
      self.assertEqual(messages[assistant_index + 2]["role"], "tool")
      self.assertEqual(messages[assistant_index + 2]["tool_call_id"], "call_open")
    finally:
      conn.close()

  def test_large_context_ledger_event_is_truncated_without_affecting_model_context(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      assembler = ContextAssembler(uow_factory=uow_factory, memory=MemoryFacade(uow_factory))

      pack = assembler.assemble(
        request=_request(
          run_id="run_ctx_large_ledger",
          scope="chat_large_ledger",
          max_tokens=12000,
          messages=[
            {"role": "user", "content": f"large ledger message {index} " + ("x" * 120)}
            for index in range(160)
          ],
        )
      )

      with UnitOfWork(conn) as uow:
        events = uow.events.list_by_run("run_ctx_large_ledger")

      self.assertEqual(events[0].event_type, RuntimeEventType.CONTEXT_BUILT)
      self.assertEqual(events[0].payload["context_pack_id"], pack.pack_id)
      self.assertTrue(events[0].payload["context_ledger_truncated"])
      self.assertEqual(events[0].payload["context_plan"]["run_id"], "run_ctx_large_ledger")
      self.assertIn("large ledger message 159", str(pack.model_context.messages))
      self.assertNotIn("large ledger message 159", str(events[0].payload))
    finally:
      conn.close()


class ContextAssemblerRunnerTests(unittest.IsolatedAsyncioTestCase):
  async def test_continuous_runner_uses_context_assembler_without_full_skill_instructions(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      registry = CapabilityRegistry()
      local_tools = LocalToolExecutor()
      catalog = AtomicCapabilityProvider()
      catalog.register(registry, local_tools)
      runtime = CapabilityRuntime(registry, PolicyEngine(), local_tools, uow_factory=uow_factory)
      provider = MockModelProvider(responses=[{"finish": True, "output": {"content": "done"}}])
      gateway = ModelGateway()
      gateway.register_provider("mock", provider)
      skill = SkillCard(
        skill_id="skill_review",
        name="Technical review",
        description="Review implementation quality.",
        when_to_use="When reviewing code or architecture.",
        instructions="FULL_REVIEW_PROCEDURE_SHOULD_NOT_BE_IN_DEFAULT_CONTEXT",
      )
      runner = ContinuousAgentRunner(
        uow_factory=uow_factory,
        model_gateway=gateway,
        capability_runtime=runtime,
        tool_catalog=catalog,
        context_assembler=ContextAssembler(uow_factory=uow_factory, memory=MemoryFacade(uow_factory)),
        skills=[skill],
      )

      result = await runner.run(
        user_message="评审一下当前实现",
        run_id="run_ctx_runner",
        scope="chat_runner",
        config=ContinuousRunnerConfig(provider_name="mock", model_ref="mock", max_turns=1),
      )

      self.assertEqual(result.status, "completed")
      context = provider.calls[0][1]
      rendered = str(context.messages)
      self.assertEqual([message["role"] for message in context.messages].count("system"), 1)
      self.assertIn("Meadow Operating Context", rendered)
      self.assertIn("skill_open", rendered)
      self.assertIn("Failure Handling", rendered)
      self.assertIn("skill_review", rendered)
      self.assertIn("Skill index is compact", rendered)
      self.assertNotIn("FULL_REVIEW_PROCEDURE", rendered)
      with UnitOfWork(conn) as uow:
        self.assertIn(RuntimeEventType.CONTEXT_BUILT, [event.event_type for event in uow.events.list_by_run("run_ctx_runner")])
    finally:
      conn.close()

  async def test_continuous_runner_can_open_skill_details_on_demand(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      skills = SkillService(uow_factory)
      skill = skills.create_interpreted_skill(
        name="Browser detail skill",
        description="Inspect browser pages.",
        when_to_use="When page inspection needs a detailed procedure.",
        instructions="FULL_BROWSER_DETAIL_PROCEDURE",
        recommended_tools=["browser_scan"],
      )
      registry = CapabilityRegistry()
      local_tools = LocalToolExecutor()
      memory = MemoryFacade(uow_factory)
      catalog = AtomicCapabilityProvider(memory=memory, uow_factory=uow_factory, skill_service=skills)
      catalog.register(registry, local_tools)
      runtime = CapabilityRuntime(
        registry,
        PolicyEngine(
          grants=[
            CapabilityGrant(
              grant_id="grant_skill_open",
              capability_id=AtomicCapabilityIds.SKILL_OPEN,
              run_id="run_ctx_skill_open",
              expires_at=utc_now() + timedelta(minutes=5),
            )
          ]
        ),
        local_tools,
        uow_factory=uow_factory,
      )
      provider = MockModelProvider(
        responses=[
          {"tool_calls": [{"name": "skill_open", "input": {"skill_id": skill.skill_id}}]},
          {"finish": True, "output": {"content": "已读取 Skill 细节。"}},
        ]
      )
      gateway = ModelGateway()
      gateway.register_provider("mock", provider)
      runner = ContinuousAgentRunner(
        uow_factory=uow_factory,
        model_gateway=gateway,
        capability_runtime=runtime,
        tool_catalog=catalog,
        context_assembler=ContextAssembler(uow_factory=uow_factory, memory=memory),
        skills=[skill],
      )

      result = await runner.run(
        user_message="按浏览器细节流程执行",
        run_id="run_ctx_skill_open",
        scope="chat_skill_open",
        config=ContinuousRunnerConfig(provider_name="mock", model_ref="mock", max_turns=2),
      )

      first_context = provider.calls[0][1]
      second_context = provider.calls[1][1]
      self.assertEqual(result.status, "completed")
      self.assertNotIn("FULL_BROWSER_DETAIL_PROCEDURE", str(first_context.messages))
      self.assertIn("FULL_BROWSER_DETAIL_PROCEDURE", str(second_context.messages))
      self.assertEqual(result.tool_calls[0].capability_id, AtomicCapabilityIds.SKILL_OPEN)
    finally:
      conn.close()


def _request(
  *,
  run_id: str,
  scope: str,
  messages=None,
  skills=None,
  tool_schemas=None,
  max_tokens: int = 4096,
):
  from agent_kernel.domain import ContextAssemblyRequest

  return ContextAssemblyRequest(
    run_id=run_id,
    scope=scope,
    model_ref="mock",
    messages=messages or [{"role": "user", "content": "帮我看看浏览器页面"}],
    agent_id="desktop_daily_agent",
    system_instructions="你是 Meadow Agent。默认用中文回复。",
    skills=skills or [],
    tool_schemas=tool_schemas or [],
    max_tokens=max_tokens,
  )


if __name__ == "__main__":
  unittest.main()
