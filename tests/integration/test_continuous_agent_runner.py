from datetime import timedelta
from pathlib import Path
import tempfile
import unittest

from agent_kernel.agents import ContinuousAgentRunner, ContinuousRunnerConfig
from agent_kernel.capabilities import AtomicCapabilityProvider, CapabilityRegistry, CapabilityRuntime
from agent_kernel.capabilities.adapters import LocalFileWorkspace, LocalToolExecutor
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


if __name__ == "__main__":
  unittest.main()
