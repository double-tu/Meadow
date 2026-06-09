import contextlib
import io
import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from datetime import timedelta

from agent_kernel.hosts.cli import main
from agent_kernel.persistence import connect_sqlite
from agent_kernel.policy import ApprovalService
from agent_kernel.runtime import unit_of_work_factory
from agent_kernel.domain import (
  NodeStepRecord,
  NodeStepStatus,
  RunState,
  RunStatus,
  RuntimeEvent,
  RuntimeEventType,
  ToolCallRecord,
  ToolCallStatus,
)
from agent_kernel.domain.base import utc_now
from agent_kernel.domain.delegation import DelegationTask


class CliHostTests(unittest.TestCase):
  def test_sample_run_inspect_replay_and_cancel(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      db = str(Path(tmp) / "kernel.sqlite")

      run_payload = self._run_cli(["--db", db, "sample-run", "--run-id", "run_cli", "--text", "hello"])
      inspect_payload = self._run_cli(["--db", db, "inspect", "run_cli"])
      replay_payload = self._run_cli(["--db", db, "replay", "run_cli"])

      self.assertTrue(run_payload["ok"])
      self.assertEqual(run_payload["status"], "completed")
      self.assertEqual(run_payload["variables"]["echo"], "hello")
      self.assertTrue(inspect_payload["ok"])
      self.assertGreaterEqual(len(inspect_payload["entries"]), 1)
      self.assertTrue(replay_payload["ok"])
      self.assertEqual(replay_payload["status"], "completed")
      self.assertEqual(replay_payload["replayed_external_calls"], 0)

  def test_approve_and_reject_commands(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      db = str(Path(tmp) / "kernel.sqlite")
      conn = connect_sqlite(db)
      try:
        approval_service = ApprovalService(unit_of_work_factory(conn))
        approve_request = approval_service.request_tool_approval("run_1", "tool.exec", "danger", {})
        reject_request = approval_service.request_tool_approval("run_1", "tool.write", "danger", {})
      finally:
        conn.close()

      approved = self._run_cli(["--db", db, "approve", approve_request.approval_id])
      rejected = self._run_cli(["--db", db, "reject", reject_request.approval_id])

      self.assertTrue(approved["ok"])
      self.assertEqual(approved["grant"]["capability_id"], "tool.exec")
      self.assertTrue(rejected["ok"])
      self.assertEqual(rejected["approval"]["status"], "rejected")

  def test_intervene_command_records_intervention_and_interrupts_run(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      db = str(Path(tmp) / "kernel.sqlite")
      conn = connect_sqlite(db)
      try:
        with unit_of_work_factory(conn)() as uow:
          uow.states.save(RunState(run_id="run_intervene", status=RunStatus.RUNNING))
      finally:
        conn.close()

      payload = self._run_cli(
        [
          "--db",
          db,
          "intervene",
          "run_intervene",
          "--content",
          "wrong direction",
          "--mode",
          "pause_and_resume",
        ]
      )

      self.assertTrue(payload["ok"])
      self.assertEqual(payload["run_status"], "interrupted")
      self.assertEqual(payload["intervention"]["content"], "wrong direction")
      self.assertIsNotNone(payload["memory_id"])

  def test_intervene_command_can_cancel_current_step_and_resume(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      db = str(Path(tmp) / "kernel.sqlite")
      conn = connect_sqlite(db)
      try:
        with unit_of_work_factory(conn)() as uow:
          uow.states.save(
            RunState(run_id="run_step_intervene", status=RunStatus.RUNNING, current_node_id="work")
          )
          uow.steps.save(
            NodeStepRecord(
              step_id="step_running",
              run_id="run_step_intervene",
              node_id="work",
              status=NodeStepStatus.RUNNING,
            )
          )
      finally:
        conn.close()

      payload = self._run_cli(
        [
          "--db",
          db,
          "intervene",
          "run_step_intervene",
          "--content",
          "stop this step and retry",
          "--mode",
          "cancel_current_step_and_resume",
        ]
      )

      conn = connect_sqlite(db)
      try:
        with unit_of_work_factory(conn)() as uow:
          state = uow.states.get("run_step_intervene")
          step = uow.steps.get("step_running")
          events = uow.events.list_by_run("run_step_intervene")
      finally:
        conn.close()

      self.assertTrue(payload["ok"])
      self.assertEqual(payload["run_status"], "running")
      self.assertEqual(payload["interrupted_step_id"], "step_running")
      self.assertEqual(state.status, RunStatus.RUNNING)
      self.assertEqual(step.status, NodeStepStatus.INTERRUPTED)
      self.assertIn(RuntimeEventType.HUMAN_INTERVENTION, [event.event_type for event in events])
      self.assertIn(RuntimeEventType.STEP_FAILED, [event.event_type for event in events])

  def test_tool_call_control_commands_record_pending_requests(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      db = str(Path(tmp) / "kernel.sqlite")
      conn = connect_sqlite(db)
      try:
        with unit_of_work_factory(conn)() as uow:
          uow.tool_calls.save(
            ToolCallRecord(
              tool_call_id="tool_call_cancel",
              run_id="run_tool_control",
              capability_id="proc.long",
              status=ToolCallStatus.RUNNING,
            )
          )
          uow.tool_calls.save(
            ToolCallRecord(
              tool_call_id="tool_call_kill",
              run_id="run_tool_control",
              capability_id="proc.long",
              status=ToolCallStatus.RUNNING,
            )
          )
      finally:
        conn.close()

      cancelled = self._run_cli(["--db", db, "cancel-tool-call", "tool_call_cancel"])
      killed = self._run_cli(["--db", db, "kill-tool-call", "tool_call_kill"])

      self.assertTrue(cancelled["ok"])
      self.assertFalse(cancelled["dispatched"])
      self.assertEqual(cancelled["requested_status"], "cancelling")
      self.assertEqual(cancelled["tool_call"]["status"], "cancelling")
      self.assertTrue(killed["ok"])
      self.assertFalse(killed["dispatched"])
      self.assertEqual(killed["requested_status"], "killing")
      self.assertEqual(killed["tool_call"]["status"], "killing")

  def test_llm_smoke_uses_environment_config(self) -> None:
    class FakeProvider:
      def __init__(self, api_key: str, base_url: str, timeout_seconds: float) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds

      async def complete(self, model_ref, context):
        return {
          "content": f"{model_ref}:{context.messages[0]['content']}",
          "base_url": self.base_url,
          "timeout": self.timeout_seconds,
        }

    with mock.patch.dict(
      "os.environ",
      {
        "AGENT_KERNEL_LLM_MODEL": "model-cli",
        "AGENT_KERNEL_LLM_API_KEY": "key-cli",
        "AGENT_KERNEL_LLM_BASE_URL": "https://llm.example/v1",
        "AGENT_KERNEL_LLM_TIMEOUT_SECONDS": "9",
      },
      clear=True,
    ), mock.patch("agent_kernel.hosts.cli.OpenAICompatibleProvider", FakeProvider):
      payload = self._run_cli(["llm-smoke", "--prompt", "ping"])

    self.assertTrue(payload["ok"])
    self.assertEqual(payload["provider"], "openai-compatible")
    self.assertEqual(payload["model"], "model-cli")
    self.assertEqual(payload["result"]["content"], "model-cli:ping")
    self.assertEqual(payload["result"]["base_url"], "https://llm.example/v1")
    self.assertEqual(payload["result"]["timeout"], 9)

  def test_llm_smoke_uses_config_file(self) -> None:
    class FakeProvider:
      def __init__(self, api_key: str, base_url: str, timeout_seconds: float) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds

      async def complete(self, model_ref, context):
        return {"content": self.api_key, "model": model_ref, "base_url": self.base_url}

    with tempfile.TemporaryDirectory() as tmp:
      config_path = Path(tmp) / "agent-kernel.toml"
      config_path.write_text(
        "\n".join(
          [
            "[llm]",
            'model = "model-file"',
            'base_url = "https://llm.file/v1"',
            'api_key_env = "TEST_LLM_KEY"',
          ]
        ),
        encoding="utf-8",
      )
      with mock.patch.dict("os.environ", {"TEST_LLM_KEY": "key-file"}, clear=True), mock.patch(
        "agent_kernel.hosts.cli.OpenAICompatibleProvider",
        FakeProvider,
      ):
        payload = self._run_cli(["--config", str(config_path), "llm-smoke", "--prompt", "ping"])

    self.assertTrue(payload["ok"])
    self.assertEqual(payload["model"], "model-file")
    self.assertEqual(payload["result"]["content"], "key-file")
    self.assertEqual(payload["result"]["base_url"], "https://llm.file/v1")

  def test_delegation_commands_use_configured_connector(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      db = str(Path(tmp) / "kernel.sqlite")
      config_path = Path(tmp) / "agent-kernel.json"
      config_path.write_text(
        json.dumps(
          {
            "agent_connectors": [
              {
                "connector_id": "worker_cli",
                "product": "worker",
                "argv": _delegation_jsonl_argv(),
                "startup_timeout_seconds": 2,
                "turn_timeout_seconds": 2,
              }
            ]
          }
        ),
        encoding="utf-8",
      )

      delegated = self._run_cli(
        [
          "--db",
          db,
          "--config",
          str(config_path),
          "delegate-agent",
          "--parent-run-id",
          "run_delegate_cli",
          "--connector-id",
          "worker_cli",
          "--task",
          "summarize repo",
          "--agent-type",
          "reviewer",
          "--metadata-json",
          '{"priority":"high"}',
        ]
      )
      task_id = delegated["delegation"]["task_id"]
      status = self._run_cli(
        [
          "--db",
          db,
          "--config",
          str(config_path),
          "delegation-status",
          "--parent-run-id",
          "run_delegate_cli",
          "--task-id",
          task_id,
        ]
      )
      hidden = self._run_cli(
        [
          "--db",
          db,
          "--config",
          str(config_path),
          "delegation-status",
          "--parent-run-id",
          "other_parent",
          "--task-id",
          task_id,
        ]
      )

    self.assertTrue(delegated["ok"])
    self.assertFalse(delegated["detached"])
    self.assertEqual(delegated["delegation"]["status"], "completed")
    self.assertEqual(delegated["delegation"]["output"]["task"], "summarize repo")
    self.assertEqual(delegated["delegation"]["agent_type"], "reviewer")
    self.assertEqual(status["delegations"][0]["status"], "completed")
    self.assertEqual(hidden["delegations"][0]["status"], "unknown")

  def test_settle_memory_command_writes_candidate_memory(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      db = str(Path(tmp) / "kernel.sqlite")
      conn = connect_sqlite(db)
      try:
        with unit_of_work_factory(conn)() as uow:
          uow.events.append(
            RuntimeEvent(
              event_type=RuntimeEventType.MEMORY_EVOLUTION_CANDIDATE,
              run_id="run_settle_cli",
              payload={
                "candidate_id": "candidate_cli",
                "note": "SOP: run focused tests after connector changes.",
                "evidence_summary": "Connector change run completed and focused tests were run successfully.",
              },
            )
          )
      finally:
        conn.close()

      payload = self._run_cli(["--db", db, "settle-memory", "run_settle_cli", "--scope", "project_cli"])

      conn = connect_sqlite(db)
      try:
        with unit_of_work_factory(conn)() as uow:
          memories = uow.memory.list_by_scope("project_cli", memory_type="procedural")
      finally:
        conn.close()

    self.assertTrue(payload["ok"])
    self.assertEqual(payload["settlements"][0]["candidate_id"], "candidate_cli")
    self.assertEqual(memories[0].content["candidate_id"], "candidate_cli")

  def test_curate_memory_command_writes_episode_and_settles_verified_candidate(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      db = str(Path(tmp) / "kernel.sqlite")
      conn = connect_sqlite(db)
      try:
        with unit_of_work_factory(conn)() as uow:
          uow.events.append(
            RuntimeEvent(
              event_type=RuntimeEventType.TOOL_CALL_COMPLETED,
              run_id="run_curate_cli",
              payload={"summary": "workspace_patch succeeded"},
            )
          )
          uow.events.append(
            RuntimeEvent(
              event_type=RuntimeEventType.MEMORY_EVOLUTION_CANDIDATE,
              run_id="run_curate_cli",
              payload={
                "candidate_id": "candidate_curate_cli",
                "scope": "project_cli",
                "note": "SOP: patch unique text and then run focused tests.",
                "evidence_summary": "workspace_patch succeeded in this run.",
              },
            )
          )
      finally:
        conn.close()

      payload = self._run_cli(["--db", db, "curate-memory", "run_curate_cli", "--scope", "project_cli"])

      conn = connect_sqlite(db)
      try:
        with unit_of_work_factory(conn)() as uow:
          episodes = uow.memory.list_by_scope("project_cli", memory_type="episodic")
          procedures = uow.memory.list_by_scope("project_cli", memory_type="procedural")
      finally:
        conn.close()

    self.assertTrue(payload["ok"])
    self.assertIsNotNone(payload["episodic_memory_id"])
    self.assertEqual(payload["settlements"][0]["candidate_id"], "candidate_curate_cli")
    self.assertEqual(episodes[0].content["kind"], "run_event_summary")
    self.assertEqual(procedures[0].content["candidate_id"], "candidate_curate_cli")

  def test_recover_delegations_command_marks_orphaned_running_failed(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      db = str(Path(tmp) / "kernel.sqlite")
      conn = connect_sqlite(db)
      try:
        with unit_of_work_factory(conn)() as uow:
          uow.interactions.save_delegation_task(
            DelegationTask(
              task_id="delegation_cli_orphaned",
              parent_run_id="run_cli_recover",
              connector_id="missing",
              connector_session_id="connector_cli_orphaned",
              task="stale child",
            )
          )
      finally:
        conn.close()

      payload = self._run_cli(["--db", db, "recover-delegations", "--reason", "restart"])

      conn = connect_sqlite(db)
      try:
        with unit_of_work_factory(conn)() as uow:
          task = uow.interactions.get_delegation_task("delegation_cli_orphaned")
      finally:
        conn.close()

    self.assertTrue(payload["ok"])
    self.assertEqual(payload["recovered"][0]["status"], "failed")
    self.assertEqual(task.error["message"], "restart")

  def test_mcp_config_commands_import_list_and_delete(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      db = str(Path(tmp) / "kernel.sqlite")
      config_path = Path(tmp) / "agent-kernel.json"
      config_path.write_text(
        json.dumps(
          {
            "mcp_servers": [
              {
                "name": "echo",
                "enabled": True,
                "agent_types": ["codex"],
                "transport": {"type": "stdio", "command": sys.executable, "args": ["-u", "-c", "pass"]},
              }
            ]
          }
        ),
        encoding="utf-8",
      )

      imported = self._run_cli(["--db", db, "--config", str(config_path), "mcp-import"])
      listed = self._run_cli(["--db", db, "mcp-list", "--enabled-only", "--agent-type", "codex"])
      deleted = self._run_cli(["--db", db, "mcp-delete", "echo"])

    self.assertTrue(imported["ok"])
    self.assertEqual(imported["mcp_servers"][0]["name"], "echo")
    self.assertEqual(listed["mcp_servers"][0]["transport"]["command"], sys.executable)
    self.assertTrue(deleted["deleted"])

  def test_schedule_commands_create_and_run_due_task(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      db = str(Path(tmp) / "kernel.sqlite")
      due_at = (utc_now() - timedelta(minutes=1)).isoformat()

      created = self._run_cli(
        [
          "--db",
          db,
          "schedule-create",
          "--task-id",
          "scheduled_cli",
          "--name",
          "CLI scheduled task",
          "--kind",
          "at",
          "--value",
          due_at,
          "--payload-json",
          '{"title":"scheduled from cli","run_id":"run_scheduled_cli"}',
        ]
      )
      ran = self._run_cli(["--db", db, "schedule-run-due"])
      listed = self._run_cli(["--db", db, "schedule-list"])

    self.assertTrue(created["ok"])
    self.assertEqual(created["scheduled_task"]["task_id"], "scheduled_cli")
    self.assertEqual(ran["triggers"][0]["result"]["task"]["run_id"], "run_scheduled_cli")
    self.assertEqual(listed["scheduled_tasks"][0]["trigger_count"], 1)

  def test_control_health_command_uses_configured_control_plane(self) -> None:
    payload = self._run_cli(["control-health"])

    self.assertTrue(payload["ok"])
    self.assertIn("browser", payload["checks"])

  @staticmethod
  def _run_cli(argv: list[str]) -> dict[str, object]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
      exit_code = main(argv)
    assert exit_code == 0
    return json.loads(stdout.getvalue())


def _delegation_jsonl_argv() -> list[str]:
  return [
    sys.executable,
    "-u",
    "-c",
    (
      "import json, sys\n"
      "for line in sys.stdin:\n"
      "    frame = json.loads(line)\n"
      "    if frame['type'] == 'start':\n"
      "        print(json.dumps({'type': 'started', 'session_id': frame['session_id']}), flush=True)\n"
      "    elif frame['type'] == 'message':\n"
      "        content = frame['content']\n"
      "        print(json.dumps({'type': 'turn', 'turn_id': 'turn_cli', "
      "'output': {'task': content['task'], 'metadata': content.get('metadata')}, 'completed': True}), flush=True)\n"
      "    elif frame['type'] == 'stop':\n"
      "        break\n"
    ),
  ]


if __name__ == "__main__":
  unittest.main()
