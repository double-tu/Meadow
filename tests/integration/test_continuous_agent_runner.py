from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest

from agent_kernel.agents import ContinuousAgentRunner, ContinuousRunnerConfig
from agent_kernel.autonomy.builtin_skills import BUILTIN_ATOMIC_SKILLS
from agent_kernel.capabilities import AtomicCapabilityProvider, CapabilityRegistry, CapabilityRuntime
from agent_kernel.capabilities.adapters import (
  ControlResult,
  ControlTarget,
  ControlWorkbench,
  FakeControlBackend,
  HTTPResponse,
  LocalFileWorkspace,
  LocalToolExecutor,
)
from agent_kernel.context import ContextAssembler
from agent_kernel.domain import CapabilityGrant, RuntimeEventType
from agent_kernel.domain.base import utc_now
from agent_kernel.memory import MemoryFacade
from agent_kernel.models import MockModelProvider, ModelGateway
from agent_kernel.observability import TraceService
from agent_kernel.persistence import UnitOfWork, connect_sqlite
from agent_kernel.policy import PolicyEngine
from agent_kernel.runtime import unit_of_work_factory


class ContinuousAgentRunnerIntegrationTests(unittest.IsolatedAsyncioTestCase):
  async def test_runner_executes_atomic_tools_and_records_memory_evolution_hook(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      conn = connect_sqlite()
      try:
        uow_factory = unit_of_work_factory(conn)
        registry = CapabilityRegistry()
        local_tools = LocalToolExecutor()
        memory = MemoryFacade(uow_factory)
        catalog = AtomicCapabilityProvider(
          file_workspace=LocalFileWorkspace([tmp]),
          memory=memory,
          default_code_cwd=tmp,
        )
        catalog.register(registry, local_tools)
        grants = [
          CapabilityGrant(
            grant_id="grant_read",
            capability_id="atom.workspace.read",
            run_id="run_continuous",
            expires_at=utc_now() + timedelta(minutes=5),
            filesystem_scope=[tmp],
          ),
          CapabilityGrant(
            grant_id="grant_write",
            capability_id="atom.workspace.write",
            run_id="run_continuous",
            expires_at=utc_now() + timedelta(minutes=5),
            filesystem_scope=[tmp],
          ),
          CapabilityGrant(
            grant_id="grant_patch",
            capability_id="atom.workspace.patch",
            run_id="run_continuous",
            expires_at=utc_now() + timedelta(minutes=5),
            filesystem_scope=[tmp],
          ),
        ]
        runtime = CapabilityRuntime(
          registry,
          PolicyEngine(grants=grants),
          local_tools,
          uow_factory=uow_factory,
        )
        note_path = str(Path(tmp) / "note.txt")
        provider = MockModelProvider(
          responses=[
            {
              "tool_calls": [
                {
                  "name": "workspace_write",
                  "input": {"path": note_path, "content": "draft", "mode": "overwrite"},
                }
              ]
            },
            {
              "tool_calls": [
                {
                  "name": "workspace_patch",
                  "input": {"path": note_path, "old_text": "draft", "new_text": "final"},
                },
                {
                  "name": "memory_checkpoint",
                  "input": {"key_info": "note.txt now contains final"},
                },
              ]
            },
            {
              "tool_calls": [
                {
                  "name": "memory_evolution_note",
                  "input": {
                    "note": "When editing text files, use unique replacement patches.",
                    "evidence_summary": "workspace_patch succeeded after workspace_write created note.txt.",
                  },
                }
              ]
            },
            {"finish": True, "output": {"summary": "done"}},
          ]
        )
        gateway = ModelGateway()
        gateway.register_provider("mock", provider)
        runner = ContinuousAgentRunner(
          uow_factory=uow_factory,
          model_gateway=gateway,
          capability_runtime=runtime,
          tool_catalog=catalog,
        )

        result = await runner.run(
          user_message="Create and refine a note.",
          run_id="run_continuous",
          scope="scope_continuous",
          config=ContinuousRunnerConfig(max_turns=6),
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.turns, 4)
        self.assertEqual(Path(note_path).read_text(encoding="utf-8"), "final")
        self.assertEqual([call.capability_id for call in result.tool_calls], [
          "atom.workspace.write",
          "atom.workspace.patch",
          "atom.memory.checkpoint",
          "atom.memory.evolution_note",
        ])
        working = memory.retrieve("scope_continuous", memory_type="working")
        self.assertEqual(working[0].content["key_info"], "note.txt now contains final")

        with UnitOfWork(conn) as uow:
          tool_calls = uow.tool_calls.list_by_run("run_continuous")
          events = uow.events.list_by_run("run_continuous")
          audit = uow.audit.list_by_run("run_continuous")

        self.assertEqual(len(tool_calls), 4)
        self.assertTrue(all(call.status == "succeeded" for call in tool_calls))
        self.assertGreaterEqual(len(audit), 8)
        self.assertIn(RuntimeEventType.MEMORY_EVOLUTION_CANDIDATE, [event.event_type for event in events])
        timeline = TraceService(uow_factory).build_timeline("run_continuous")
        self.assertGreaterEqual(len(timeline.entries), 4)
      finally:
        conn.close()

  async def test_runner_compacts_large_tool_results_before_event_logging(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      conn = connect_sqlite()
      try:
        uow_factory = unit_of_work_factory(conn)
        registry = CapabilityRegistry()
        local_tools = LocalToolExecutor()
        catalog = AtomicCapabilityProvider(file_workspace=LocalFileWorkspace([tmp]))
        catalog.register(registry, local_tools)
        note_path = str(Path(tmp) / "large.txt")
        Path(note_path).write_text("x" * 12000, encoding="utf-8")
        runtime = CapabilityRuntime(
          registry,
          PolicyEngine(
            grants=[
              CapabilityGrant(
                grant_id="grant_large_read",
                capability_id="atom.workspace.read",
                run_id="run_large_tool_result",
                expires_at=utc_now() + timedelta(minutes=5),
                filesystem_scope=[tmp],
              )
            ]
          ),
          local_tools,
          uow_factory=uow_factory,
        )
        provider = MockModelProvider(
          responses=[
            {
              "tool_calls": [
                {
                  "name": "workspace_read",
                  "input": {"path": note_path, "show_line_numbers": False},
                }
              ]
            },
            {"finish": True, "output": {"summary": "large output handled"}},
          ]
        )
        gateway = ModelGateway()
        gateway.register_provider("mock", provider)
        runner = ContinuousAgentRunner(
          uow_factory=uow_factory,
          model_gateway=gateway,
          capability_runtime=runtime,
          tool_catalog=catalog,
        )

        result = await runner.run(
          user_message="Read the large file.",
          run_id="run_large_tool_result",
          scope="scope_large_tool_result",
          config=ContinuousRunnerConfig(max_turns=4),
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.output["summary"], "large output handled")
        self.assertTrue(result.tool_calls[0].output["_artifactized"])
        artifact_id = result.tool_calls[0].output["artifact_id"]
        tool_result_messages = [
          message
          for message in provider.calls[1][1].messages
          if message.get("role") == "tool"
        ]
        tool_result_message = json.loads(tool_result_messages[-1]["content"])["output"]
        self.assertTrue(tool_result_message["_artifactized"])
        self.assertEqual(tool_result_message["artifact_id"], artifact_id)
        with UnitOfWork(conn) as uow:
          artifact = uow.artifacts.get(artifact_id)
          artifact_metadata = uow.artifacts.get_metadata(artifact_id)
          events = [
            event
            for event in uow.events.list_by_run("run_large_tool_result")
            if event.event_type == RuntimeEventType.AGENT_TURN_COMPLETED
          ]
        self.assertIsNotNone(artifact)
        self.assertIn('"content"', artifact_metadata["content"])
        self.assertTrue(events[0].payload["output"]["tool_results"][0]["output"]["_artifactized"])
      finally:
        conn.close()

  async def test_runner_preserves_original_goal_after_tool_result_turn(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      conn = connect_sqlite()
      try:
        uow_factory = unit_of_work_factory(conn)
        registry = CapabilityRegistry()
        local_tools = LocalToolExecutor()
        catalog = AtomicCapabilityProvider(file_workspace=LocalFileWorkspace([tmp]))
        catalog.register(registry, local_tools)
        note_path = str(Path(tmp) / "note.txt")
        Path(note_path).write_text("current browser targets", encoding="utf-8")
        runtime = CapabilityRuntime(
          registry,
          PolicyEngine(
            grants=[
              CapabilityGrant(
                grant_id="grant_goal_read",
                capability_id="atom.workspace.read",
                run_id="run_goal_preserved",
                expires_at=utc_now() + timedelta(minutes=5),
                filesystem_scope=[tmp],
              )
            ]
          ),
          local_tools,
          uow_factory=uow_factory,
        )
        provider = MockModelProvider(
          responses=[
            {
              "tool_calls": [
                {
                  "name": "workspace_read",
                  "input": {"path": note_path},
                }
              ]
            },
            {"finish": True, "output": {"summary": "continued original goal"}},
          ]
        )
        gateway = ModelGateway()
        gateway.register_provider("mock", provider)
        runner = ContinuousAgentRunner(
          uow_factory=uow_factory,
          model_gateway=gateway,
          capability_runtime=runtime,
          tool_catalog=catalog,
        )

        await runner.run(
          user_message="打开小红书并查看推送内容",
          run_id="run_goal_preserved",
          scope="scope_goal_preserved",
          config=ContinuousRunnerConfig(max_turns=4),
        )

        second_messages = provider.calls[1][1].messages
        anchor_messages = [
          message
          for message in second_messages
          if isinstance(message.get("content"), dict) and message["content"].get("type") == "working_memory_anchor"
        ]
        self.assertTrue(anchor_messages)
        self.assertEqual(anchor_messages[-1]["content"]["original_user_goal"], "打开小红书并查看推送内容")
        self.assertIn("turn 1: workspace_read ok", anchor_messages[-1]["content"]["action_history"])
        self.assertEqual(second_messages[-4]["content"], "打开小红书并查看推送内容")
        self.assertEqual(second_messages[-3]["role"], "assistant")
        self.assertTrue(second_messages[-3]["tool_calls"])
        self.assertEqual(second_messages[-2]["role"], "tool")
        tool_result_content = json.loads(second_messages[-2]["content"])
        self.assertEqual(tool_result_content["tool_name"], "workspace_read")
        runtime_context = second_messages[-1]["content"]
        self.assertEqual(runtime_context["type"], "runtime_context")
        self.assertEqual(runtime_context["original_user_goal"], "打开小红书并查看推送内容")
        self.assertEqual(runtime_context["action_history"], ["turn 1: workspace_read ok"])
        self.assertIn("Continue working on original_user_goal", runtime_context["instruction"])
        self.assertIn("avoid repeating failed or already-completed steps", runtime_context["instruction"])
      finally:
        conn.close()

  async def test_runner_persists_anchor_snapshot_for_context_reinjection(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      conn = connect_sqlite()
      try:
        uow_factory = unit_of_work_factory(conn)
        registry = CapabilityRegistry()
        local_tools = LocalToolExecutor()
        memory = MemoryFacade(uow_factory)
        catalog = AtomicCapabilityProvider(file_workspace=LocalFileWorkspace([tmp]), memory=memory)
        catalog.register(registry, local_tools)
        note_path = str(Path(tmp) / "note.txt")
        Path(note_path).write_text("state from tool", encoding="utf-8")
        runtime = CapabilityRuntime(
          registry,
          PolicyEngine(
            grants=[
              CapabilityGrant(
                grant_id="grant_anchor_read",
                capability_id="atom.workspace.read",
                run_id="run_anchor_snapshot",
                expires_at=utc_now() + timedelta(minutes=5),
                filesystem_scope=[tmp],
              )
            ]
          ),
          local_tools,
          uow_factory=uow_factory,
        )
        provider = MockModelProvider(
          responses=[
            {"tool_calls": [{"name": "workspace_read", "input": {"path": note_path}}]},
            {"finish": True, "output": {"summary": "used persisted anchor"}},
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
          memory=memory,
        )

        result = await runner.run(
          user_message="读取文件后继续完成原始任务",
          run_id="run_anchor_snapshot",
          scope="scope_anchor_snapshot",
          config=ContinuousRunnerConfig(max_turns=3),
        )

        self.assertEqual(result.status, "completed")
        working = memory.retrieve("scope_anchor_snapshot", memory_type="working", limit=10)
        snapshots = [item for item in working if item.content.get("kind") == "run_anchor_snapshot"]
        self.assertTrue(snapshots)
        self.assertEqual(snapshots[0].content["original_user_goal"], "读取文件后继续完成原始任务")
        self.assertIn("turn 1: workspace_read ok", snapshots[0].content["action_history"])
        second_rendered = str(provider.calls[1][1].messages)
        self.assertIn("run_anchor_snapshot", second_rendered)
        self.assertIn("读取文件后继续完成原始任务", second_rendered)
      finally:
        conn.close()

  async def test_runner_prunes_tool_surface_to_relevant_browser_skill(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      registry = CapabilityRegistry()
      local_tools = LocalToolExecutor()
      catalog = AtomicCapabilityProvider()
      catalog.register(registry, local_tools)
      runtime = CapabilityRuntime(
        registry,
        PolicyEngine(grants=[]),
        local_tools,
        uow_factory=uow_factory,
      )
      provider = MockModelProvider(responses=[{"finish": True, "output": {"summary": "ok"}}])
      gateway = ModelGateway()
      gateway.register_provider("mock", provider)
      runner = ContinuousAgentRunner(
        uow_factory=uow_factory,
        model_gateway=gateway,
        capability_runtime=runtime,
        tool_catalog=catalog,
        skills=list(BUILTIN_ATOMIC_SKILLS),
      )

      await runner.run(
        user_message="帮我用浏览器打开页面刷新一下，看看最新推荐帖子",
        run_id="run_browser_tool_surface",
        scope="scope_browser_tool_surface",
        config=ContinuousRunnerConfig(max_turns=2),
      )

      tool_names = {schema["function"]["name"] for schema in provider.calls[0][1].tool_schemas}
      self.assertIn("browser_scan", tool_names)
      self.assertIn("browser_execute_js", tool_names)
      self.assertIn("browser_navigate", tool_names)
      self.assertIn("http_request", tool_names)
      self.assertIn("skill_open", tool_names)
      self.assertNotIn("workspace_write", tool_names)
      self.assertNotIn("desktop_click", tool_names)
      self.assertLess(len(tool_names), len(catalog.tool_schemas()))
      navigate_schema = next(schema for schema in provider.calls[0][1].tool_schemas if schema["function"]["name"] == "browser_navigate")
      self.assertIn("create a new owned tab", navigate_schema["function"]["description"])
      selected_skill_messages = [
        message
        for message in provider.calls[0][1].messages
        if isinstance(message.get("content"), dict) and message["content"].get("type") == "selected_skills"
      ]
      self.assertEqual(selected_skill_messages[0]["content"]["skills"][0]["skill_id"], "builtin.atomic.web_research")
    finally:
      conn.close()

  async def test_runner_injects_research_ledger_after_browser_observation(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      registry = CapabilityRegistry()
      local_tools = LocalToolExecutor()
      catalog = AtomicCapabilityProvider()
      catalog.register(registry, local_tools)
      backend = FakeControlBackend()
      backend.register_target(
        ControlTarget(
          target_id="tab_1",
          kind="browser",
          label="Search",
          metadata={"url": "https://search.example/?q=plugin"},
        )
      )
      backend.register_response(
        "browser",
        "inspect",
        ControlResult(
          ok=True,
          output={
            "page": {
              "title": "Search Results",
              "url": "https://search.example/?q=plugin",
              "text": "Search result snippets are only candidates.",
              "search_results": [
                {
                  "title": "Official Pricing",
                  "href": "https://example.com/pricing",
                  "snippet": "Subscription details",
                }
              ],
              "links": [{"text": "Forum discussion", "href": "https://forum.example/topic"}],
            }
          },
        ),
      )
      runtime = CapabilityRuntime(
        registry,
        PolicyEngine(grants=[]),
        local_tools,
        control_workbench=ControlWorkbench(backend),
        uow_factory=uow_factory,
      )
      provider = MockModelProvider(
        responses=[
          {"tool_calls": [{"name": "browser_scan", "input": {"target_id": "tab_1"}}]},
          {"finish": True, "output": {"summary": "will continue from ledger"}},
        ]
      )
      gateway = ModelGateway()
      gateway.register_provider("mock", provider)
      runner = ContinuousAgentRunner(
        uow_factory=uow_factory,
        model_gateway=gateway,
        capability_runtime=runtime,
        tool_catalog=catalog,
        context_assembler=ContextAssembler(uow_factory=uow_factory, memory=MemoryFacade(uow_factory)),
        skills=list(BUILTIN_ATOMIC_SKILLS),
      )

      await runner.run(
        user_message="帮我深度搜索这个插件的订阅版本",
        run_id="run_research_ledger",
        scope="scope_research_ledger",
        config=ContinuousRunnerConfig(max_turns=3),
      )

      second_messages = provider.calls[1][1].messages
      context_messages = [
        message
        for message in second_messages
        if message.get("role") == "user"
        and message.get("name") == "context"
        and isinstance(message.get("content"), str)
      ]
      self.assertTrue(context_messages)
      rendered_context = context_messages[-1]["content"]
      self.assertIn("https://example.com/pricing", rendered_context)
      self.assertIn("search_results_need_source_open", rendered_context)
      tool_result_payload = second_messages[-1]["content"]["research_ledger"]
      self.assertEqual(tool_result_payload["evidence"][0]["url"], "https://search.example/?q=plugin")
    finally:
      conn.close()

  async def test_runner_retries_empty_model_result_before_finishing(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      registry = CapabilityRegistry()
      local_tools = LocalToolExecutor()
      catalog = AtomicCapabilityProvider()
      catalog.register(registry, local_tools)
      runtime = CapabilityRuntime(registry, PolicyEngine(grants=[]), local_tools, uow_factory=uow_factory)
      provider = MockModelProvider(
        responses=[
          {"finish": True, "output": {}},
          {"finish": True, "output": {"content": "恢复后的结果"}},
        ]
      )
      gateway = ModelGateway()
      gateway.register_provider("mock", provider)
      runner = ContinuousAgentRunner(
        uow_factory=uow_factory,
        model_gateway=gateway,
        capability_runtime=runtime,
        tool_catalog=catalog,
      )

      result = await runner.run(
        user_message="帮我执行一个需要结果的任务",
        run_id="run_empty_model_result",
        scope="scope_empty_model_result",
        config=ContinuousRunnerConfig(max_turns=4),
      )

      self.assertEqual(result.status, "completed")
      self.assertEqual(result.output["content"], "恢复后的结果")
      self.assertEqual(len(provider.calls), 2)
      retry_content = provider.calls[1][1].messages[-1]["content"]
      self.assertEqual(retry_content["type"], "model_repair")
      self.assertIn("Blank response", retry_content["instruction"])
    finally:
      conn.close()

  async def test_runner_executes_text_tool_use_protocol_blocks(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      registry = CapabilityRegistry()
      local_tools = LocalToolExecutor()
      catalog = AtomicCapabilityProvider(http_client=_StaticHTTPClient())
      catalog.register(registry, local_tools)
      runtime = CapabilityRuntime(
        registry,
        PolicyEngine(
          grants=[
            CapabilityGrant(
              grant_id="grant_text_tool_http",
              capability_id="atom.http.request",
              run_id="run_text_tool_protocol",
              expires_at=utc_now() + timedelta(minutes=5),
            )
          ]
        ),
        local_tools,
        uow_factory=uow_factory,
      )
      provider = MockModelProvider(
        responses=[
          {
            "content": (
              "<summary>准备请求网页</summary>\n"
              '<tool_use>{"name":"http_request","arguments":{"url":"https://www.xiaohongshu.com/explore"}}</tool_use>'
            )
          },
          {"finish": True, "output": {"content": "已根据工具结果完成。"}},
        ]
      )
      gateway = ModelGateway()
      gateway.register_provider("mock", provider)
      runner = ContinuousAgentRunner(
        uow_factory=uow_factory,
        model_gateway=gateway,
        capability_runtime=runtime,
        tool_catalog=catalog,
      )

      result = await runner.run(
        user_message="用文本工具协议请求页面",
        run_id="run_text_tool_protocol",
        scope="scope_text_tool_protocol",
        config=ContinuousRunnerConfig(max_turns=4),
      )

      self.assertEqual(result.status, "completed")
      self.assertEqual(result.tool_calls[0].name, "http_request")
      self.assertTrue(result.tool_calls[0].ok)
      self.assertEqual(result.output["content"], "已根据工具结果完成。")
    finally:
      conn.close()

  async def test_runner_repairs_invalid_text_tool_protocol(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      registry = CapabilityRegistry()
      local_tools = LocalToolExecutor()
      catalog = AtomicCapabilityProvider()
      catalog.register(registry, local_tools)
      runtime = CapabilityRuntime(registry, PolicyEngine(grants=[]), local_tools, uow_factory=uow_factory)
      provider = MockModelProvider(
        responses=[
          {"content": '<tool_use>{"name":"http_request","arguments":</tool_use>'},
          {"finish": True, "output": {"content": "已重新生成有效回复。"}},
        ]
      )
      gateway = ModelGateway()
      gateway.register_provider("mock", provider)
      runner = ContinuousAgentRunner(
        uow_factory=uow_factory,
        model_gateway=gateway,
        capability_runtime=runtime,
        tool_catalog=catalog,
      )

      result = await runner.run(
        user_message="测试坏工具协议修复",
        run_id="run_bad_text_tool_protocol",
        scope="scope_bad_text_tool_protocol",
        config=ContinuousRunnerConfig(max_turns=4),
      )

      self.assertEqual(result.status, "completed")
      retry_content = provider.calls[1][1].messages[-1]["content"]
      self.assertEqual(retry_content["diagnostics"]["reason"], "tool_protocol_error")
      self.assertIn("valid JSON", retry_content["instruction"])
    finally:
      conn.close()

  async def test_runner_repairs_truncated_no_tool_result_before_finishing(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      registry = CapabilityRegistry()
      local_tools = LocalToolExecutor()
      catalog = AtomicCapabilityProvider()
      catalog.register(registry, local_tools)
      runtime = CapabilityRuntime(registry, PolicyEngine(grants=[]), local_tools, uow_factory=uow_factory)
      provider = MockModelProvider(
        responses=[
          {"finish": True, "output": {"content": "中间结果还没完成 max_tokens !!!]"}, "finish_reason": "max_tokens"},
          {"finish": True, "output": {"content": "分步恢复后的结果"}},
        ]
      )
      gateway = ModelGateway()
      gateway.register_provider("mock", provider)
      runner = ContinuousAgentRunner(
        uow_factory=uow_factory,
        model_gateway=gateway,
        capability_runtime=runtime,
        tool_catalog=catalog,
      )

      result = await runner.run(
        user_message="帮我执行一个不能半途截断的任务",
        run_id="run_truncated_model_result",
        scope="scope_truncated_model_result",
        config=ContinuousRunnerConfig(max_turns=4),
      )

      self.assertEqual(result.status, "completed")
      self.assertEqual(result.output["content"], "分步恢复后的结果")
      retry_content = provider.calls[1][1].messages[-1]["content"]
      self.assertEqual(retry_content["diagnostics"]["reason"], "max_tokens_limit")
      self.assertIn("smaller steps", retry_content["instruction"])
    finally:
      conn.close()

  async def test_runner_returns_fallback_content_when_max_turns_exceeded(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      registry = CapabilityRegistry()
      local_tools = LocalToolExecutor()
      catalog = AtomicCapabilityProvider(http_client=_StaticHTTPClient())
      catalog.register(registry, local_tools)
      runtime = CapabilityRuntime(
        registry,
        PolicyEngine(
          grants=[
            CapabilityGrant(
              grant_id="grant_http_fallback",
              capability_id="atom.http.request",
              run_id="run_max_turns_fallback",
              expires_at=utc_now() + timedelta(minutes=5),
            )
          ]
        ),
        local_tools,
        uow_factory=uow_factory,
      )
      repeated_response = {
        "tool_calls": [
          {
            "name": "http_request",
            "input": {"url": "https://www.xiaohongshu.com/explore"},
          }
        ]
      }
      provider = MockModelProvider(responses=[repeated_response, repeated_response, repeated_response, repeated_response])
      gateway = ModelGateway()
      gateway.register_provider("mock", provider)
      runner = ContinuousAgentRunner(
        uow_factory=uow_factory,
        model_gateway=gateway,
        capability_runtime=runtime,
        tool_catalog=catalog,
      )

      result = await runner.run(
        user_message="打开小红书并查看推送内容",
        run_id="run_max_turns_fallback",
        scope="scope_max_turns_fallback",
        config=ContinuousRunnerConfig(max_turns=1),
      )

      self.assertEqual(result.status, "max_turns_exceeded")
      self.assertIn("页面条目/候选内容包括", result.output["content"])
      self.assertIn("云南大理避暑很舒服", result.output["content"])
    finally:
      conn.close()

  async def test_runner_returns_diagnostic_when_model_finishes_empty_without_tools(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      registry = CapabilityRegistry()
      local_tools = LocalToolExecutor()
      catalog = AtomicCapabilityProvider()
      catalog.register(registry, local_tools)
      runtime = CapabilityRuntime(registry, PolicyEngine(), local_tools, uow_factory=uow_factory)
      provider = MockModelProvider(
        responses=[
          {"finish": True, "output": {}},
          {"finish": True, "output": {}},
          {"finish": True, "output": {}},
        ]
      )
      gateway = ModelGateway()
      gateway.register_provider("mock", provider)
      runner = ContinuousAgentRunner(
        uow_factory=uow_factory,
        model_gateway=gateway,
        capability_runtime=runtime,
        tool_catalog=catalog,
      )

      result = await runner.run(
        user_message="帮我完成一个任务",
        run_id="run_empty_finish",
        scope="scope_empty_finish",
        config=ContinuousRunnerConfig(max_turns=4),
      )

      self.assertEqual(result.status, "failed")
      self.assertIn("没有返回可展示内容", result.output["content"])
      self.assertEqual(result.output["diagnostics"]["type"], "empty_model_result")
      self.assertEqual(result.output["diagnostics"]["turn"], 3)
    finally:
      conn.close()

  async def test_runner_fallback_summarizes_browser_execute_js_structured_cards(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      registry = CapabilityRegistry()
      local_tools = LocalToolExecutor()
      catalog = AtomicCapabilityProvider()
      catalog.register(registry, local_tools)
      runtime = CapabilityRuntime(
        registry,
        PolicyEngine(
          grants=[
            CapabilityGrant(
              grant_id="grant_browser_js_cards",
              capability_id="atom.browser.execute_js",
              run_id="run_browser_js_cards",
              expires_at=utc_now() + timedelta(minutes=5),
            )
          ]
        ),
        local_tools,
        control_workbench=ControlWorkbench(_BrowserExecuteJsFeedBackend()),
        uow_factory=uow_factory,
      )
      provider = MockModelProvider(
        responses=[
          {
            "tool_calls": [
              {
                "name": "browser_execute_js",
                "input": {"target_id": "tab_feed", "code": "return extractCards()"},
              }
            ]
          }
        ]
      )
      gateway = ModelGateway()
      gateway.register_provider("mock", provider)
      runner = ContinuousAgentRunner(
        uow_factory=uow_factory,
        model_gateway=gateway,
        capability_runtime=runtime,
        tool_catalog=catalog,
      )

      result = await runner.run(
        user_message="通过浏览器打开小红书，刷新一下获取最新推荐帖子",
        run_id="run_browser_js_cards",
        scope="scope_browser_js_cards",
        config=ContinuousRunnerConfig(max_turns=1),
      )

      self.assertEqual(result.status, "max_turns_exceeded")
      self.assertIn("已通过浏览器获取到页面内容", result.output["content"])
      self.assertIn("深圳周末咖啡地图", result.output["content"])
      self.assertIn("夏天通勤穿搭", result.output["content"])
    finally:
      conn.close()

  async def test_runner_fallback_reports_anti_spider_http_result(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      registry = CapabilityRegistry()
      local_tools = LocalToolExecutor()
      catalog = AtomicCapabilityProvider(http_client=_AntiSpiderHTTPClient())
      catalog.register(registry, local_tools)
      runtime = CapabilityRuntime(
        registry,
        PolicyEngine(
          grants=[
            CapabilityGrant(
              grant_id="grant_http_antispider",
              capability_id="atom.http.request",
              run_id="run_antispider_fallback",
              expires_at=utc_now() + timedelta(minutes=5),
            )
          ]
        ),
        local_tools,
        uow_factory=uow_factory,
      )
      provider = MockModelProvider(
        responses=[
          {
            "tool_calls": [
              {
                "name": "http_request",
                "input": {"url": "http://www.sogou.com/antispider/?m=1"},
              }
            ]
          },
          {"content": ""},
        ]
      )
      gateway = ModelGateway()
      gateway.register_provider("mock", provider)
      runner = ContinuousAgentRunner(
        uow_factory=uow_factory,
        model_gateway=gateway,
        capability_runtime=runtime,
        tool_catalog=catalog,
      )

      result = await runner.run(
        user_message="搜索公开网页",
        run_id="run_antispider_fallback",
        scope="scope_antispider_fallback",
        config=ContinuousRunnerConfig(max_turns=4),
      )

      self.assertEqual(result.status, "completed")
      self.assertIn("反爬/验证码页面", result.output["content"])
      self.assertIn("HTTP 结果不能当作有效搜索内容", result.output["content"])
    finally:
      conn.close()

  async def test_runner_stops_repeated_identical_tool_calls(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      registry = CapabilityRegistry()
      local_tools = LocalToolExecutor()
      catalog = AtomicCapabilityProvider(http_client=_StaticHTTPClient())
      catalog.register(registry, local_tools)
      runtime = CapabilityRuntime(
        registry,
        PolicyEngine(
          grants=[
            CapabilityGrant(
              grant_id="grant_repeated_http",
              capability_id="atom.http.request",
              run_id="run_repeated_guard",
              expires_at=utc_now() + timedelta(minutes=5),
            )
          ]
        ),
        local_tools,
        uow_factory=uow_factory,
      )
      repeated_response = {
        "tool_calls": [
          {
            "name": "http_request",
            "input": {"url": "https://www.xiaohongshu.com/explore"},
          }
        ]
      }
      provider = MockModelProvider(
        responses=[
          repeated_response,
          repeated_response,
          repeated_response,
          repeated_response,
          {"finish": True, "output": {"content": "已停止重复读取，并说明了阻塞原因。"}},
        ]
      )
      gateway = ModelGateway()
      gateway.register_provider("mock", provider)
      runner = ContinuousAgentRunner(
        uow_factory=uow_factory,
        model_gateway=gateway,
        capability_runtime=runtime,
        tool_catalog=catalog,
      )

      result = await runner.run(
        user_message="反复读取同一页面",
        run_id="run_repeated_guard",
        scope="scope_repeated_guard",
        config=ContinuousRunnerConfig(max_turns=8),
      )

      self.assertEqual(result.status, "completed")
      self.assertEqual(result.output["content"], "已停止重复读取，并说明了阻塞原因。")
      self.assertEqual(result.tool_calls[-1].error["type"], "repeated_tool_call_guard")
      repair_tool_messages = [
        message
        for message in provider.calls[4][1].messages
        if message.get("role") == "tool"
      ]
      repair_content = json.loads(repair_tool_messages[-1]["content"])
      self.assertEqual(repair_content["error"]["type"], "repeated_tool_call_guard")
      self.assertIn("Repeated identical tool call was blocked", repair_content["repair_hint"])
      with UnitOfWork(conn) as uow:
        persisted_calls = uow.tool_calls.list_by_run("run_repeated_guard")
      self.assertEqual(len(persisted_calls), 3)
    finally:
      conn.close()

  async def test_runner_injects_no_progress_hook_for_repeated_low_information_actions(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      registry = CapabilityRegistry()
      local_tools = LocalToolExecutor()
      catalog = AtomicCapabilityProvider()
      catalog.register(registry, local_tools)
      runtime = CapabilityRuntime(
        registry,
        PolicyEngine(
          grants=[
            CapabilityGrant(
              grant_id="grant_browser_tabs_only_progress",
              capability_id="atom.browser.scan",
              run_id="run_no_progress_hook",
              expires_at=utc_now() + timedelta(minutes=5),
            )
          ]
        ),
        local_tools,
        control_workbench=ControlWorkbench(_BrowserTabsOnlyBackend()),
        uow_factory=uow_factory,
      )
      provider = MockModelProvider(
        responses=[
          {"tool_calls": [{"name": "browser_scan", "input": {"tabs_only": True}}]},
          {"tool_calls": [{"name": "skill_open", "input": {"skill_id": "builtin.atomic.web_research"}}]},
          {"tool_calls": [{"name": "browser_scan", "input": {"tabs_only": True}}]},
          {"finish": True, "output": {"content": "已收到无进展提示，准备更换策略。"}},
        ]
      )
      gateway = ModelGateway()
      gateway.register_provider("mock", provider)
      runner = ContinuousAgentRunner(
        uow_factory=uow_factory,
        model_gateway=gateway,
        capability_runtime=runtime,
        tool_catalog=catalog,
      )

      result = await runner.run(
        user_message="帮我用浏览器打开小红书刷新并获取最新帖子",
        run_id="run_no_progress_hook",
        scope="scope_no_progress_hook",
        config=ContinuousRunnerConfig(max_turns=5),
      )

      self.assertEqual(result.status, "completed")
      hook_content = provider.calls[3][1].messages[-1]["content"]
      self.assertTrue(hook_content["execution_hooks"])
      self.assertEqual(hook_content["execution_hooks"][0]["hook"], "no_progress_repeated_low_information")
      self.assertIn("Do not repeat", hook_content["execution_hooks"][0]["repair_hint"])
      self.assertIn("execution_hook warning:no_progress", hook_content["action_history"][-1])
    finally:
      conn.close()

  async def test_runner_max_turns_returns_structured_no_progress_diagnostic(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      registry = CapabilityRegistry()
      local_tools = LocalToolExecutor()
      catalog = AtomicCapabilityProvider()
      catalog.register(registry, local_tools)
      runtime = CapabilityRuntime(
        registry,
        PolicyEngine(
          grants=[
            CapabilityGrant(
              grant_id="grant_browser_tabs_only_terminal",
              capability_id="atom.browser.scan",
              run_id="run_no_progress_terminal",
              expires_at=utc_now() + timedelta(minutes=5),
            )
          ]
        ),
        local_tools,
        control_workbench=ControlWorkbench(_BrowserTabsOnlyBackend()),
        uow_factory=uow_factory,
      )
      repeated_scan = {"tool_calls": [{"name": "browser_scan", "input": {"tabs_only": True}}]}
      provider = MockModelProvider(responses=[repeated_scan, repeated_scan, repeated_scan])
      gateway = ModelGateway()
      gateway.register_provider("mock", provider)
      runner = ContinuousAgentRunner(
        uow_factory=uow_factory,
        model_gateway=gateway,
        capability_runtime=runtime,
        tool_catalog=catalog,
      )

      result = await runner.run(
        user_message="帮我用浏览器打开小红书刷新并获取最新帖子",
        run_id="run_no_progress_terminal",
        scope="scope_no_progress_terminal",
        config=ContinuousRunnerConfig(max_turns=3),
      )

      self.assertEqual(result.status, "max_turns_exceeded")
      self.assertEqual(result.output["diagnostics"]["type"], "execution_terminal_diagnostic")
      self.assertEqual(result.output["diagnostics"]["original_goal"], "帮我用浏览器打开小红书刷新并获取最新帖子")
      self.assertGreaterEqual(len(result.output["diagnostics"]["no_progress_causes"]), 1)
      self.assertIn("未完成原因", result.output["content"])
      self.assertIn("建议下一步", result.output["content"])
    finally:
      conn.close()

  async def test_runner_stop_hook_repairs_empty_completion_text(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      registry = CapabilityRegistry()
      local_tools = LocalToolExecutor()
      catalog = AtomicCapabilityProvider()
      catalog.register(registry, local_tools)
      runtime = CapabilityRuntime(registry, PolicyEngine(grants=[]), local_tools, uow_factory=uow_factory)
      provider = MockModelProvider(
        responses=[
          {"finish": True, "output": {"content": "日常 Agent 已完成运行，但没有返回可展示内容。"}},
          {"finish": True, "output": {"content": "已改为输出具体失败原因和下一步。"}},
        ]
      )
      gateway = ModelGateway()
      gateway.register_provider("mock", provider)
      runner = ContinuousAgentRunner(
        uow_factory=uow_factory,
        model_gateway=gateway,
        capability_runtime=runtime,
        tool_catalog=catalog,
      )

      result = await runner.run(
        user_message="帮我执行一个需要明确结果的任务",
        run_id="run_stop_hook_empty_completion",
        scope="scope_stop_hook_empty_completion",
        config=ContinuousRunnerConfig(max_turns=4),
      )

      self.assertEqual(result.status, "completed")
      self.assertEqual(result.output["content"], "已改为输出具体失败原因和下一步。")
      repair_content = provider.calls[1][1].messages[-1]["content"]
      self.assertEqual(repair_content["diagnostics"]["type"], "stop_hook_blocking")
      self.assertEqual(repair_content["execution_hooks"][0]["hook"], "stop_hook_invalid_empty_final")
    finally:
      conn.close()

  async def test_runner_fallback_summarizes_browser_page_observation(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      registry = CapabilityRegistry()
      local_tools = LocalToolExecutor()
      catalog = AtomicCapabilityProvider()
      catalog.register(registry, local_tools)
      runtime = CapabilityRuntime(
        registry,
        PolicyEngine(
          grants=[
            CapabilityGrant(
              grant_id="grant_browser_page_observation",
              capability_id="atom.browser.scan",
              run_id="run_browser_page_observation",
              expires_at=utc_now() + timedelta(minutes=5),
            )
          ]
        ),
        local_tools,
        control_workbench=ControlWorkbench(_BrowserObservationBackend()),
        uow_factory=uow_factory,
      )
      provider = MockModelProvider(
        responses=[
          {
            "tool_calls": [
              {
                "name": "browser_scan",
                "input": {"target_id": "tab_search"},
              }
            ]
          },
          {"content": ""},
        ]
      )
      gateway = ModelGateway()
      gateway.register_provider("mock", provider)
      runner = ContinuousAgentRunner(
        uow_factory=uow_factory,
        model_gateway=gateway,
        capability_runtime=runtime,
        tool_catalog=catalog,
      )

      result = await runner.run(
        user_message="用浏览器搜索",
        run_id="run_browser_page_observation",
        scope="scope_browser_page_observation",
        config=ContinuousRunnerConfig(max_turns=4),
      )

      self.assertEqual(result.status, "completed")
      self.assertIn("已通过浏览器读取到页面内容", result.output["content"])
      self.assertIn("深圳 小学英语老师_百度搜索", result.output["content"])
      self.assertIn("南外老师", result.output["content"])
      self.assertIn("https://source.example/wanghexizi", result.output["content"])
      action_history = provider.calls[1][1].messages[-1]["content"]["action_history"]
      self.assertEqual(len(action_history), 1)
      self.assertIn("turn 1: browser_scan ok", action_history[0])
      self.assertIn("target=tab_search", action_history[0])
      self.assertIn("page=深圳 小学英语老师_百度搜索", action_history[0])
    finally:
      conn.close()

  async def test_runner_fallback_prioritizes_browser_feed_titles(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      registry = CapabilityRegistry()
      local_tools = LocalToolExecutor()
      catalog = AtomicCapabilityProvider()
      catalog.register(registry, local_tools)
      runtime = CapabilityRuntime(
        registry,
        PolicyEngine(
          grants=[
            CapabilityGrant(
              grant_id="grant_browser_feed_observation",
              capability_id="atom.browser.scan",
              run_id="run_browser_feed_observation",
              expires_at=utc_now() + timedelta(minutes=5),
            )
          ]
        ),
        local_tools,
        control_workbench=ControlWorkbench(_BrowserFeedObservationBackend()),
        uow_factory=uow_factory,
      )
      provider = MockModelProvider(
        responses=[
          {
            "tool_calls": [
              {
                "name": "browser_scan",
                "input": {"target_id": "tab_feed"},
              }
            ]
          }
        ]
      )
      gateway = ModelGateway()
      gateway.register_provider("mock", provider)
      runner = ContinuousAgentRunner(
        uow_factory=uow_factory,
        model_gateway=gateway,
        capability_runtime=runtime,
        tool_catalog=catalog,
      )

      result = await runner.run(
        user_message="看看推荐帖子",
        run_id="run_browser_feed_observation",
        scope="scope_browser_feed_observation",
        config=ContinuousRunnerConfig(max_turns=1),
      )

      self.assertEqual(result.status, "max_turns_exceeded")
      self.assertIn("已通过浏览器获取到页面内容", result.output["content"])
      self.assertIn("云南大理避暑很舒服", result.output["content"])
      self.assertIn("当了三十年的班主任", result.output["content"])
    finally:
      conn.close()

  async def test_runner_fallback_summarizes_browser_execute_js_cards(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      registry = CapabilityRegistry()
      local_tools = LocalToolExecutor()
      catalog = AtomicCapabilityProvider()
      catalog.register(registry, local_tools)
      runtime = CapabilityRuntime(
        registry,
        PolicyEngine(
          grants=[
            CapabilityGrant(
              grant_id="grant_browser_js_cards",
              capability_id="atom.browser.execute_js",
              run_id="run_browser_js_cards",
              expires_at=utc_now() + timedelta(minutes=5),
            )
          ]
        ),
        local_tools,
        control_workbench=ControlWorkbench(_BrowserExecuteJSCardsBackend()),
        uow_factory=uow_factory,
      )
      provider = MockModelProvider(
        responses=[
          {
            "tool_calls": [
              {
                "name": "browser_execute_js",
                "input": {"target_id": "tab_feed", "code": "return extractCards()"},
              }
            ]
          }
        ]
      )
      gateway = ModelGateway()
      gateway.register_provider("mock", provider)
      runner = ContinuousAgentRunner(
        uow_factory=uow_factory,
        model_gateway=gateway,
        capability_runtime=runtime,
        tool_catalog=catalog,
      )

      result = await runner.run(
        user_message="刷新一下获取最新推荐帖子",
        run_id="run_browser_js_cards",
        scope="scope_browser_js_cards",
        config=ContinuousRunnerConfig(max_turns=1),
      )

      self.assertEqual(result.status, "max_turns_exceeded")
      self.assertIn("已通过浏览器获取到页面内容", result.output["content"])
      self.assertIn("深圳周末去哪玩", result.output["content"])
      self.assertIn("AI 工具流整理", result.output["content"])
    finally:
      conn.close()

  async def test_runner_finalizes_after_sufficient_browser_feed_evidence(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      registry = CapabilityRegistry()
      local_tools = LocalToolExecutor()
      catalog = AtomicCapabilityProvider()
      catalog.register(registry, local_tools)
      runtime = CapabilityRuntime(
        registry,
        PolicyEngine(
          grants=[
            CapabilityGrant(
              grant_id="grant_browser_feed_finalize",
              capability_id="atom.browser.scan",
              run_id="run_browser_feed_finalize",
              expires_at=utc_now() + timedelta(minutes=5),
            )
          ]
        ),
        local_tools,
        control_workbench=ControlWorkbench(_BrowserRichFeedObservationBackend()),
        uow_factory=uow_factory,
      )
      provider = MockModelProvider(
        responses=[
          {"tool_calls": [{"name": "browser_scan", "input": {"target_id": "tab_feed"}}]},
          {"finish": True, "output": {"content": "已整理推荐内容。"}},
        ]
      )
      gateway = ModelGateway()
      gateway.register_provider("mock", provider)
      runner = ContinuousAgentRunner(
        uow_factory=uow_factory,
        model_gateway=gateway,
        capability_runtime=runtime,
        tool_catalog=catalog,
        skills=list(BUILTIN_ATOMIC_SKILLS),
      )

      result = await runner.run(
        user_message="帮我用浏览器刷新页面，看看最新推荐帖子",
        run_id="run_browser_feed_finalize",
        scope="scope_browser_feed_finalize",
        config=ContinuousRunnerConfig(max_turns=4),
      )

      self.assertEqual(result.status, "completed")
      self.assertEqual(result.output["content"], "已整理推荐内容。")
      self.assertEqual(provider.calls[1][1].tool_schemas, [])
      tool_result_content = provider.calls[1][1].messages[-1]["content"]
      self.assertIn("already satisfy the user goal", tool_result_content["instruction"])
    finally:
      conn.close()


class _StaticHTTPClient:
  def request(self, method, url, *, headers=None, body=None, timeout_seconds=None):
    return HTTPResponse(
      status=200,
      headers={"content-type": "text/html"},
      body=(
        '<html><head><title>小红书 - 你的生活兴趣社区</title></head>'
        '<body><script>{"displayTitle":"云南大理避暑很舒服","displayTitle":"当了三十年的班主任"}</script></body></html>'
      ),
      url=url,
    )


class _AntiSpiderHTTPClient:
  def request(self, method, url, *, headers=None, body=None, timeout_seconds=None):
    return HTTPResponse(
      status=200,
      headers={"content-type": "text/html"},
      body="<html><head><title>Sogou Antispider</title></head><body>antispider verify</body></html>",
      url=url,
    )


class _BrowserObservationBackend:
  def list_targets(self, kind=None):
    return []

  async def execute(self, command):
    return ControlResult(
      ok=True,
      output={
        "active_target_id": "tab_search",
        "page": {
          "title": "深圳 小学英语老师_百度搜索",
          "url": "https://www.baidu.com/s?wd=深圳+小学英语老师",
          "visible_cards": ["课程思政 项目育人", "四年级项目式学习课例"],
          "links": [{"text": "课程思政 项目育人", "href": "https://source.example/wanghexizi"}],
          "search_results": [
            {
              "title": "课程思政 项目育人",
              "href": "https://source.example/wanghexizi",
              "snippet": "南外老师。",
            }
          ],
          "text": "称赞老师。",
        },
      },
    )


class _BrowserTabsOnlyBackend:
  def list_targets(self, kind=None):
    return []

  async def execute(self, command):
    return ControlResult(
      ok=True,
      output={
        "active_target_id": "tab_feed",
        "targets": [
          {
            "target_id": "tab_feed",
            "kind": "browser",
            "label": "小红书 - 你的生活兴趣社区",
            "metadata": {"url": "https://www.xiaohongshu.com/explore"},
          },
          {
            "target_id": "tab_workbench",
            "kind": "browser",
            "label": "Meadow 桌面工作台",
            "metadata": {"url": "http://127.0.0.1:4180/"},
          },
        ],
      },
    )


class _BrowserFeedObservationBackend:
  def list_targets(self, kind=None):
    return []

  async def execute(self, command):
    return ControlResult(
      ok=True,
      output={
        "active_target_id": "tab_feed",
        "page": {
          "title": "小红书 - 你的生活兴趣社区",
          "url": "https://www.xiaohongshu.com/explore",
          "feed_titles": ["云南大理避暑很舒服", "当了三十年的班主任"],
          "visible_cards": ["《用户协议》", "登录后推荐更懂你的笔记"],
          "text": "登录后推荐更懂你的笔记",
        },
      },
    )


class _BrowserRichFeedObservationBackend:
  def list_targets(self, kind=None):
    return []

  async def execute(self, command):
    return ControlResult(
      ok=True,
      output={
        "active_target_id": "tab_feed",
        "page": {
          "title": "推荐页",
          "url": "https://example.test/feed",
          "feed_titles": ["第一条推荐", "第二条推荐", "第三条推荐", "第四条推荐"],
          "visible_cards": ["第一条推荐 作者A", "第二条推荐 作者B", "第三条推荐 作者C"],
        },
      },
    )


class _BrowserExecuteJsFeedBackend:
  def list_targets(self, kind=None):
    return []

  async def execute(self, command):
    return ControlResult(
      ok=True,
      output={
        "target_id": command.target_id,
        "result": {
          "data": [
            {
              "title": "深圳周末咖啡地图",
              "author": "城市漫游者",
              "url": "https://www.xiaohongshu.com/explore/a",
            },
            {
              "title": "夏天通勤穿搭",
              "author": "日常记录",
              "metrics": {"likes": 128},
            },
          ]
        },
      },
    )


class _BrowserExecuteJSCardsBackend:
  def list_targets(self, kind=None):
    return []

  async def execute(self, command):
    return ControlResult(
      ok=True,
      output={
        "target_id": command.target_id,
        "result": {
          "js_return": [
            {"title": "深圳周末去哪玩", "author": "本地生活"},
            {"title": "AI 工具流整理", "author": "效率笔记"},
          ]
        },
      },
    )


if __name__ == "__main__":
  unittest.main()
