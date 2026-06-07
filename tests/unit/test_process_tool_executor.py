import asyncio
import os
import sys
import tempfile
import unittest

from agent_kernel.capabilities.adapters import PosixProcessGroupIsolationStrategy, ProcessToolExecutor


class ProcessToolExecutorTests(unittest.IsolatedAsyncioTestCase):
  async def test_process_tool_success(self) -> None:
    executor = ProcessToolExecutor()
    executor.register(
      "proc.echo",
      [sys.executable, "-c", "print('hello')"],
      timeout_seconds=2,
    )

    result = await executor.call("proc.echo", {})

    self.assertTrue(result.ok)
    self.assertEqual(result.output["stdout"], "hello\n")
    self.assertEqual(result.output["returncode"], 0)

  async def test_process_tool_timeout(self) -> None:
    executor = ProcessToolExecutor()
    executor.register(
      "proc.sleep",
      [sys.executable, "-c", "import time; time.sleep(2)"],
      timeout_seconds=0.05,
    )

    result = await executor.call("proc.sleep", {})

    self.assertFalse(result.ok)
    self.assertEqual(result.error["type"], "timeout")

  async def test_process_tool_streams_stdout_and_stderr(self) -> None:
    executor = ProcessToolExecutor()
    executor.register(
      "proc.stream",
      [
        sys.executable,
        "-u",
        "-c",
        (
          "import sys\n"
          "print('out-1')\n"
          "print('err-1', file=sys.stderr)\n"
        ),
      ],
      timeout_seconds=2,
    )

    events = [event async for event in executor.stream("proc.stream", {}, tool_call_id="tool_stream")]

    self.assertEqual(events[0].event, "started")
    self.assertEqual(events[0].tool_call_id, "tool_stream")
    self.assertIn(("stdout", "out-1\n"), [(event.event, event.data) for event in events])
    self.assertIn(("stderr", "err-1\n"), [(event.event, event.data) for event in events])
    self.assertEqual(events[-1].event, "exited")
    self.assertEqual(events[-1].returncode, 0)

  async def test_process_tool_stream_timeout_terminates_process(self) -> None:
    executor = ProcessToolExecutor()
    executor.register(
      "proc.stream_sleep",
      [sys.executable, "-u", "-c", "import time; print('before', flush=True); time.sleep(2)"],
      timeout_seconds=0.05,
    )

    events = [event async for event in executor.stream("proc.stream_sleep", {})]

    self.assertIn(("stdout", "before\n"), [(event.event, event.data) for event in events])
    self.assertEqual(events[-1].event, "timeout")
    self.assertEqual(events[-1].error["type"], "timeout")

  @unittest.skipUnless(os.name == "posix", "POSIX process groups are only available on POSIX.")
  async def test_process_group_cancel_signals_child_process(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      child_pid_path = os.path.join(tmp, "child.pid")
      child_terminated_path = os.path.join(tmp, "child.terminated")
      child_code = (
        "import pathlib, signal, sys, time\n"
        f"pathlib.Path({child_pid_path!r}).write_text(str(__import__('os').getpid()))\n"
        "def stop(signum, frame):\n"
        f"  pathlib.Path({child_terminated_path!r}).write_text('terminated')\n"
        "  sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "time.sleep(30)\n"
      )
      parent_code = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        "time.sleep(30)\n"
      )
      executor = ProcessToolExecutor(isolation_strategy=PosixProcessGroupIsolationStrategy())
      executor.register("proc.tree", [sys.executable, "-c", parent_code], timeout_seconds=30)

      task = asyncio.create_task(executor.call("proc.tree", {}, tool_call_id="tool_tree"))
      await self._wait_for_file(child_pid_path)

      status = await executor.cancel("tool_tree", grace_seconds=1)
      result = await task
      await self._wait_for_file(child_terminated_path)

      self.assertEqual(status, "cancelled")
      self.assertFalse(result.ok)
      self.assertEqual(result.error["type"], "cancelled")
      with open(child_terminated_path, encoding="utf-8") as handle:
        self.assertEqual(handle.read(), "terminated")

  @staticmethod
  async def _wait_for_file(path: str) -> None:
    for _ in range(100):
      if os.path.exists(path):
        return
      await asyncio.sleep(0.01)
    raise AssertionError(f"Timed out waiting for file: {path}")


if __name__ == "__main__":
  unittest.main()
