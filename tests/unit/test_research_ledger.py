import unittest

from agent_kernel.agents.research_ledger import ResearchLedger
from agent_kernel.agents.conversation_runner import ContinuousToolCallRecord


class ResearchLedgerTests(unittest.TestCase):
  def test_ledger_tracks_browser_evidence_and_candidates(self) -> None:
    ledger = ResearchLedger.from_goal("帮我深度搜索一个插件订阅版本")
    record = ContinuousToolCallRecord(
      name="browser_scan",
      capability_id="atom.browser.scan",
      input={"target_id": "tab_1"},
      ok=True,
      output={
        "page": {
          "title": "搜索结果",
          "url": "https://search.example/?q=test",
          "text": "结果页摘要",
          "search_results": [
            {
              "title": "官方定价",
              "href": "https://example.com/pricing",
              "snippet": "包含订阅说明",
            }
          ],
          "links": [{"text": "讨论帖", "href": "https://forum.example/topic"}],
        }
      },
    )

    ledger.observe_records([record], turn=1)

    payload = ledger.to_model_payload()
    self.assertIsNotNone(payload)
    self.assertEqual(payload["evidence"][0]["url"], "https://search.example/?q=test")
    self.assertEqual(payload["candidate_sources"][0]["url"], "https://example.com/pricing")
    self.assertIn("search_results_need_source_open", payload["strategy_notes"][0])


if __name__ == "__main__":
  unittest.main()
