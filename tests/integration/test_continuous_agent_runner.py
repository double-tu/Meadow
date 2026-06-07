from datetime import timedelta
from pathlib import Path
import tempfile
import unittest

from agent_kernel.agents import ContinuousAgentRunner, ContinuousRunnerConfig
from agent_kernel.capabilities import AtomicCapabilityProvider, CapabilityRegistry, CapabilityRuntime
from agent_kernel.capabilities.adapters import HTTPResponse, LocalFileWorkspace, LocalToolExecutor
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
      provider = MockModelProvider(
        responses=[
          {
            "tool_calls": [
              {
                "name": "http_request",
                "input": {"url": "https://www.xiaohongshu.com/explore"},
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


if __name__ == "__main__":
  unittest.main()
