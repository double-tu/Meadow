import sys
import unittest

from agent_kernel.capabilities.adapters import ProcessToolExecutor


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


if __name__ == "__main__":
  unittest.main()
