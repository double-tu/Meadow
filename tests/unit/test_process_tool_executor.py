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


if __name__ == "__main__":
  unittest.main()

