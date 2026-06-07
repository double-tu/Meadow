import contextlib
import io
import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from agent_kernel.hosts.cli import main
from agent_kernel.persistence import connect_sqlite
from agent_kernel.policy import ApprovalService
from agent_kernel.runtime import unit_of_work_factory
from agent_kernel.domain import RunState, RunStatus, ToolCallRecord, ToolCallStatus


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

  @staticmethod
  def _run_cli(argv: list[str]) -> dict[str, object]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
      exit_code = main(argv)
    assert exit_code == 0
    return json.loads(stdout.getvalue())


if __name__ == "__main__":
  unittest.main()
