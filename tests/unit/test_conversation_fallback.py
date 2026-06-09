import unittest

from agent_kernel.agents.conversation_runner import ContinuousToolCallRecord, _fallback_output_from_tool_calls


class ConversationFallbackTests(unittest.TestCase):
  def test_fallback_does_not_treat_browser_targets_as_feed_content(self) -> None:
    output = _fallback_output_from_tool_calls(
      [
        ContinuousToolCallRecord(
          name="browser_navigate",
          capability_id="atom.browser.navigate",
          input={"payload": {"url": "https://example.test/search"}},
          ok=False,
          output={
            "result": {
              "data": [
                {
                  "id": "tab_existing",
                  "url": "https://unrelated.example",
                  "title": "Unrelated Existing Tab",
                  "active": True,
                }
              ]
            },
            "url": "https://example.test/search",
          },
          error={"type": "browser_target_creation_unconfirmed"},
        ),
        ContinuousToolCallRecord(
          name="browser_scan",
          capability_id="atom.browser.scan",
          input={"tabs_only": True},
          ok=True,
          output={
            "targets": [
              {
                "target_id": "tab_existing",
                "label": "Unrelated Existing Tab",
                "metadata": {"url": "https://unrelated.example"},
              }
            ]
          },
        ),
      ]
    )

    content = output["content"]
    self.assertIn("日常 Agent 达到最大执行轮次", content)
    self.assertIn("browser_target_creation_unconfirmed", content)
    self.assertNotIn("已通过浏览器获取到页面内容", content)
    self.assertNotIn("Unrelated Existing Tab", content)


if __name__ == "__main__":
  unittest.main()
