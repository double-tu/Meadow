from datetime import timedelta
from pathlib import Path
import tempfile
import unittest

from agent_kernel.agents import ContinuousAgentRunner, ContinuousRunnerConfig
from agent_kernel.capabilities import AtomicCapabilityProvider, CapabilityRegistry, CapabilityRuntime
from agent_kernel.capabilities.adapters import ControlResult, ControlWorkbench, HTTPResponse, LocalFileWorkspace, LocalToolExecutor
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
                  "input": {"note": "When editing text files, use unique replacement patches."},
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
        self.assertTrue(result.tool_calls[0].output["content"].startswith("x"))
        tool_result_message = provider.calls[1][1].messages[-1]["content"]["results"][0]["output"]
        self.assertTrue(tool_result_message["_truncated"])
        with UnitOfWork(conn) as uow:
          events = [
            event
            for event in uow.events.list_by_run("run_large_tool_result")
            if event.event_type == RuntimeEventType.AGENT_TURN_COMPLETED
          ]
        self.assertTrue(events[0].payload["output"]["tool_results"][0]["output"]["_truncated"])
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
        self.assertEqual(second_messages[-2]["content"], "打开小红书并查看推送内容")
        tool_result_content = second_messages[-1]["content"]
        self.assertEqual(tool_result_content["original_user_goal"], "打开小红书并查看推送内容")
        self.assertIn("Continue working on original_user_goal", tool_result_content["instruction"])
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
      self.assertIn("看到的推荐内容包括", result.output["content"])
      self.assertIn("云南大理避暑很舒服", result.output["content"])
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
        user_message="反复读取同一页面",
        run_id="run_repeated_guard",
        scope="scope_repeated_guard",
        config=ContinuousRunnerConfig(max_turns=8),
      )

      self.assertEqual(result.status, "max_turns_exceeded")
      self.assertEqual(result.tool_calls[-1].error["type"], "repeated_tool_call_guard")
      with UnitOfWork(conn) as uow:
        persisted_calls = uow.tool_calls.list_by_run("run_repeated_guard")
      self.assertEqual(len(persisted_calls), 3)
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
      self.assertIn("深圳 小学英语老师 王禾溪子_百度搜索", result.output["content"])
      self.assertIn("南外(集团)滨海小学英语科组长王禾溪子老师", result.output["content"])
      self.assertIn("https://source.example/wanghexizi", result.output["content"])
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
          "title": "深圳 小学英语老师 王禾溪子_百度搜索",
          "url": "https://www.baidu.com/s?wd=深圳+小学英语老师+王禾溪子",
          "visible_cards": ["课程思政 项目育人", "四年级项目式学习课例"],
          "links": [{"text": "课程思政 项目育人", "href": "https://source.example/wanghexizi"}],
          "search_results": [
            {
              "title": "课程思政 项目育人",
              "href": "https://source.example/wanghexizi",
              "snippet": "南外(集团)滨海小学英语科组长王禾溪子老师。",
            }
          ],
          "text": "称赞王禾溪子老师课堂的亮点纷呈。南外(集团)滨海小学英语科组长王禾溪子老师。",
        },
      },
    )


if __name__ == "__main__":
  unittest.main()
