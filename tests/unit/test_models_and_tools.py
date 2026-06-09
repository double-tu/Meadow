import unittest
from unittest import mock
import tempfile
from pathlib import Path

from agent_kernel.capabilities.adapters import LocalToolExecutor
from agent_kernel.config import LLMConfig
from agent_kernel.domain.capability import ToolResult
from agent_kernel.domain.context import ModelContext
from agent_kernel.models import (
  AnthropicMessagesProvider,
  GeminiProvider,
  MockModelProvider,
  ModelContextSanitizer,
  ModelGateway,
  ModelToolProtocolAdapter,
  OpenAICompatibleProvider,
)


class ModelsAndToolsTests(unittest.IsolatedAsyncioTestCase):
  async def test_mock_model_gateway_returns_scripted_response(self) -> None:
    gateway = ModelGateway()
    provider = MockModelProvider(responses=[{"content": "ok"}])
    gateway.register_provider("mock", provider)

    result = await gateway.complete(
      provider_name="mock",
      model_ref="mock-small",
      context=ModelContext(messages=[{"role": "user", "content": "hi"}]),
    )

    self.assertEqual(result, {"content": "ok"})
    self.assertEqual(len(provider.calls), 1)

  def test_model_context_sanitizer_merges_and_drops_invalid_leading_messages(self) -> None:
    sanitizer = ModelContextSanitizer()

    context = sanitizer.sanitize(
      ModelContext(
        messages=[
          {"role": "assistant", "content": "orphan"},
          {"role": "system", "content": "rules"},
          {"role": "weird", "content": "first user"},
          {"role": "user", "content": "second user"},
          {"role": "assistant", "content": "answer"},
          {"role": "assistant", "content": "more"},
        ]
      )
    )

    self.assertEqual(context.messages[0], {"role": "system", "content": "rules"})
    self.assertEqual(context.messages[1], {"role": "user", "content": "first user\nsecond user"})
    self.assertEqual(context.messages[2], {"role": "assistant", "content": "answer\nmore"})

  def test_model_context_sanitizer_preserves_standard_tool_transcript_fields(self) -> None:
    sanitizer = ModelContextSanitizer()

    context = sanitizer.sanitize(
      ModelContext(
        messages=[
          {"role": "user", "content": "read file"},
          {
            "role": "assistant",
            "content": "",
            "tool_calls": [
              {
                "id": "call_1",
                "type": "function",
                "function": {"name": "workspace_read", "arguments": '{"path":"a.txt"}'},
              }
            ],
          },
          {"role": "tool", "tool_call_id": "call_1", "name": "workspace_read", "content": '{"ok":true}'},
        ]
      )
    )

    self.assertEqual(context.messages[1]["tool_calls"][0]["id"], "call_1")
    self.assertEqual(context.messages[2]["tool_call_id"], "call_1")
    self.assertEqual(context.messages[2]["name"], "workspace_read")

  def test_model_context_sanitizer_keeps_named_context_user_separate(self) -> None:
    sanitizer = ModelContextSanitizer()

    context = sanitizer.sanitize(
      ModelContext(
        messages=[
          {"role": "system", "content": "rules"},
          {"role": "user", "name": "context", "content": "retrieved context"},
          {"role": "user", "content": "actual user request"},
        ]
      )
    )

    self.assertEqual(context.messages[1], {"role": "user", "name": "context", "content": "retrieved context"})
    self.assertEqual(context.messages[2], {"role": "user", "content": "actual user request"})

  def test_model_tool_protocol_adapter_parses_text_tool_use_blocks(self) -> None:
    adapter = ModelToolProtocolAdapter()

    adapted = adapter.adapt(
      {
        "content": (
          "<summary>准备请求网页</summary>\n"
          '<tool_use>{"name":"http_request","arguments":{"url":"https://example.test"}}</tool_use>'
        )
      }
    )

    self.assertEqual(adapted.result["tool_calls"][0]["name"], "http_request")
    self.assertEqual(adapted.result["tool_calls"][0]["input"]["url"], "https://example.test")
    self.assertNotIn("<tool_use>", adapted.result["output"]["content"])

  def test_model_tool_protocol_adapter_reports_bad_text_tool_json(self) -> None:
    adapter = ModelToolProtocolAdapter()

    adapted = adapter.adapt({"content": '<tool_use>{"name":"http_request","arguments":</tool_use>'})

    self.assertEqual(adapted.result["protocol_errors"][0]["type"], "tool_json_parse_error")
    self.assertNotIn("tool_calls", adapted.result)

  async def test_local_tool_executor_returns_tool_result(self) -> None:
    executor = LocalToolExecutor()
    executor.register("echo", lambda input: ToolResult(ok=True, output={"echo": input["text"]}))

    result = await executor.call("echo", {"text": "hello"})

    self.assertTrue(result.ok)
    self.assertEqual(result.output["echo"], "hello")

  async def test_local_tool_executor_normalizes_plain_return_values(self) -> None:
    executor = LocalToolExecutor()
    executor.register("raw", lambda input: {"echo": input["text"]})

    result = await executor.call("raw", {"text": "hello"})

    self.assertTrue(result.ok)
    self.assertEqual(result.output, {"echo": "hello"})
    self.assertEqual(result.status, "succeeded")

  async def test_openai_compatible_provider_uses_chat_completion_payload(self) -> None:
    calls = []

    def transport(url, headers, payload, timeout_seconds):
      calls.append((url, headers, payload, timeout_seconds))
      return {
        "choices": [{"message": {"content": "real response"}}],
        "usage": {"total_tokens": 3},
      }

    provider = OpenAICompatibleProvider(
      api_key="key",
      base_url="https://llm.example/v1/",
      timeout_seconds=7,
      transport=transport,
    )

    result = await provider.complete(
      "model-a",
      ModelContext(messages=[{"role": "user", "name": "context", "content": {"question": "hi"}}]),
    )

    self.assertEqual(result["content"], "real response")
    self.assertEqual(result["usage"], {"total_tokens": 3})
    self.assertEqual(calls[0][0], "https://llm.example/v1/chat/completions")
    self.assertEqual(calls[0][1]["Authorization"], "Bearer key")
    self.assertEqual(calls[0][2]["model"], "model-a")
    self.assertEqual(calls[0][2]["messages"][0]["name"], "context")
    self.assertEqual(calls[0][2]["messages"][0]["content"], '{"question": "hi"}')
    self.assertEqual(calls[0][3], 7)

  async def test_openai_compatible_provider_passes_and_parses_tool_calls(self) -> None:
    calls = []

    def transport(url, headers, payload, timeout_seconds):
      calls.append(payload)
      return {
        "choices": [
          {
            "message": {
              "content": "",
              "tool_calls": [
                {
                  "id": "call_1",
                  "type": "function",
                  "function": {
                    "name": "http_request",
                    "arguments": '{"url":"https://example.test"}',
                  },
                }
              ],
            }
          }
        ]
      }

    provider = OpenAICompatibleProvider(api_key="key", transport=transport)

    result = await provider.complete(
      "model-tools",
      ModelContext(
        messages=[{"role": "user", "content": "search"}],
        tool_schemas=[
          {
            "type": "function",
            "function": {
              "name": "http_request",
              "parameters": {"type": "object"},
            },
          }
        ],
      ),
    )

    self.assertEqual(calls[0]["tools"][0]["function"]["name"], "http_request")
    self.assertEqual(result["tool_calls"][0]["function"]["name"], "http_request")

  async def test_openai_compatible_provider_preserves_tool_transcript_payload(self) -> None:
    calls = []

    def transport(url, headers, payload, timeout_seconds):
      calls.append(payload)
      return {"choices": [{"message": {"content": "done"}}]}

    provider = OpenAICompatibleProvider(api_key="key", transport=transport)

    await provider.complete(
      "model-tools",
      ModelContext(
        messages=[
          {"role": "user", "content": "read file"},
          {
            "role": "assistant",
            "content": None,
            "tool_calls": [
              {
                "id": "call_1",
                "type": "function",
                "function": {"name": "workspace_read", "arguments": '{"path":"a.txt"}'},
              }
            ],
          },
          {"role": "tool", "tool_call_id": "call_1", "name": "workspace_read", "content": '{"ok":true}'},
        ]
      ),
    )

    self.assertIsNone(calls[0]["messages"][1]["content"])
    self.assertEqual(calls[0]["messages"][1]["tool_calls"][0]["id"], "call_1")
    self.assertEqual(calls[0]["messages"][2]["tool_call_id"], "call_1")
    self.assertEqual(calls[0]["messages"][2]["name"], "workspace_read")

  async def test_gemini_provider_maps_tools_and_function_calls(self) -> None:
    calls = []

    def transport(url, headers, payload, timeout_seconds):
      calls.append((url, headers, payload, timeout_seconds))
      return {
        "candidates": [
          {
            "content": {
              "parts": [
                {"functionCall": {"name": "browser_scan", "args": {"tabs_only": True}}},
                {"text": "checking"},
              ]
            }
          }
        ],
        "usageMetadata": {"totalTokenCount": 8},
      }

    provider = GeminiProvider(api_key="gemini-key", timeout_seconds=9, transport=transport)

    result = await provider.complete(
      "gemini-2.5-pro",
      ModelContext(
        messages=[{"role": "system", "content": "中文"}, {"role": "user", "content": "看浏览器"}],
        tool_schemas=[
          {
            "type": "function",
            "function": {"name": "browser_scan", "description": "scan", "parameters": {"type": "object"}},
          }
        ],
      ),
    )

    self.assertIn("models/gemini-2.5-pro:generateContent", calls[0][0])
    self.assertIn("key=gemini-key", calls[0][0])
    self.assertEqual(calls[0][2]["tools"][0]["functionDeclarations"][0]["name"], "browser_scan")
    self.assertEqual(result["tool_calls"][0]["name"], "browser_scan")
    self.assertEqual(result["tool_calls"][0]["input"], {"tabs_only": True})
    self.assertEqual(result["usage"], {"totalTokenCount": 8})

  async def test_gemini_provider_maps_standard_tool_transcript_to_function_parts(self) -> None:
    calls = []

    def transport(url, headers, payload, timeout_seconds):
      calls.append(payload)
      return {"candidates": [{"content": {"parts": [{"text": "done"}]}}]}

    provider = GeminiProvider(api_key="gemini-key", transport=transport)

    await provider.complete(
      "gemini-2.5-pro",
      ModelContext(
        messages=[
          {"role": "user", "content": "read file"},
          {
            "role": "assistant",
            "content": "",
            "tool_calls": [
              {
                "id": "call_1",
                "type": "function",
                "function": {"name": "workspace_read", "arguments": '{"path":"a.txt"}'},
              }
            ],
          },
          {"role": "tool", "tool_call_id": "call_1", "name": "workspace_read", "content": '{"ok":true}'},
        ]
      ),
    )

    self.assertEqual(calls[0]["contents"][1]["role"], "model")
    self.assertEqual(calls[0]["contents"][1]["parts"][0]["functionCall"]["name"], "workspace_read")
    self.assertEqual(calls[0]["contents"][1]["parts"][0]["functionCall"]["args"], {"path": "a.txt"})
    self.assertEqual(calls[0]["contents"][2]["role"], "user")
    self.assertEqual(calls[0]["contents"][2]["parts"][0]["functionResponse"]["name"], "workspace_read")
    self.assertEqual(calls[0]["contents"][2]["parts"][0]["functionResponse"]["response"], {"ok": True})

  async def test_anthropic_provider_maps_tools_and_tool_use(self) -> None:
    calls = []

    def transport(url, headers, payload, timeout_seconds):
      calls.append((url, headers, payload, timeout_seconds))
      return {
        "content": [
          {"type": "tool_use", "name": "task_status", "input": {"run_id": "run_1"}},
          {"type": "text", "text": "done"},
        ],
        "usage": {"input_tokens": 3, "output_tokens": 4},
      }

    provider = AnthropicMessagesProvider(api_key="anthropic-key", base_url="https://claude.example/v1", transport=transport)

    result = await provider.complete(
      "claude-sonnet-4",
      ModelContext(
        messages=[{"role": "system", "content": "中文"}, {"role": "user", "content": "状态"}],
        tool_schemas=[
          {
            "type": "function",
            "function": {"name": "task_status", "description": "status", "parameters": {"type": "object"}},
          }
        ],
      ),
    )

    self.assertEqual(calls[0][0], "https://claude.example/v1/messages")
    self.assertEqual(calls[0][1]["x-api-key"], "anthropic-key")
    self.assertEqual(calls[0][2]["tools"][0]["input_schema"], {"type": "object"})
    self.assertEqual(result["tool_calls"][0]["name"], "task_status")
    self.assertEqual(result["tool_calls"][0]["input"], {"run_id": "run_1"})
    self.assertEqual(result["content"], "done")

  async def test_anthropic_provider_maps_standard_tool_transcript_to_blocks(self) -> None:
    calls = []

    def transport(url, headers, payload, timeout_seconds):
      calls.append(payload)
      return {"content": [{"type": "text", "text": "done"}]}

    provider = AnthropicMessagesProvider(api_key="anthropic-key", transport=transport)

    await provider.complete(
      "claude-sonnet-4",
      ModelContext(
        messages=[
          {"role": "user", "content": "read file"},
          {
            "role": "assistant",
            "content": "",
            "tool_calls": [
              {
                "id": "call_1",
                "type": "function",
                "function": {"name": "workspace_read", "arguments": '{"path":"a.txt"}'},
              }
            ],
          },
          {"role": "tool", "tool_call_id": "call_1", "name": "workspace_read", "content": '{"ok":true}'},
        ]
      ),
    )

    self.assertEqual(calls[0]["messages"][1]["role"], "assistant")
    self.assertEqual(calls[0]["messages"][1]["content"][0]["type"], "tool_use")
    self.assertEqual(calls[0]["messages"][1]["content"][0]["id"], "call_1")
    self.assertEqual(calls[0]["messages"][1]["content"][0]["name"], "workspace_read")
    self.assertEqual(calls[0]["messages"][1]["content"][0]["input"], {"path": "a.txt"})
    self.assertEqual(calls[0]["messages"][2]["role"], "user")
    self.assertEqual(calls[0]["messages"][2]["content"][0]["type"], "tool_result")
    self.assertEqual(calls[0]["messages"][2]["content"][0]["tool_use_id"], "call_1")

  def test_llm_config_reads_environment(self) -> None:
    with mock.patch.dict(
      "os.environ",
      {
        "AGENT_KERNEL_LLM_MODEL": "model-env",
        "AGENT_KERNEL_LLM_API_KEY": "key-env",
        "AGENT_KERNEL_LLM_BASE_URL": "https://llm.example/v1/",
        "AGENT_KERNEL_LLM_TIMEOUT_SECONDS": "12",
      },
      clear=True,
    ):
      config = LLMConfig.from_env()

    self.assertEqual(config.provider, "openai-compatible")
    self.assertEqual(config.model, "model-env")
    self.assertEqual(config.api_key, "key-env")
    self.assertEqual(config.base_url, "https://llm.example/v1")
    self.assertEqual(config.timeout_seconds, 12)

  def test_llm_config_reports_missing_required_values(self) -> None:
    with mock.patch.dict("os.environ", {}, clear=True):
      with self.assertRaisesRegex(ValueError, "Missing LLM configuration"):
        LLMConfig.from_env()

  def test_llm_config_reads_toml_file_and_api_key_env(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      config_path = Path(tmp) / "agent-kernel.toml"
      config_path.write_text(
        "\n".join(
          [
            "[llm]",
            'provider = "openai-compatible"',
            'model = "model-file"',
            'base_url = "https://llm.example/v1/"',
            'api_key_env = "TEST_LLM_KEY"',
            "timeout_seconds = 15",
          ]
        ),
        encoding="utf-8",
      )
      with mock.patch.dict("os.environ", {"TEST_LLM_KEY": "key-file"}, clear=True):
        config = LLMConfig.from_file(config_path)

    self.assertEqual(config.provider, "openai-compatible")
    self.assertEqual(config.model, "model-file")
    self.assertEqual(config.api_key, "key-file")
    self.assertEqual(config.base_url, "https://llm.example/v1")
    self.assertEqual(config.timeout_seconds, 15)

  def test_llm_config_reads_json_file(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      config_path = Path(tmp) / "agent-kernel.json"
      config_path.write_text(
        '{"llm":{"model":"model-json","api_key":"key-json","timeout_seconds":5}}',
        encoding="utf-8",
      )
      config = LLMConfig.from_file(config_path)

    self.assertEqual(config.model, "model-json")
    self.assertEqual(config.api_key, "key-json")
    self.assertEqual(config.base_url, "https://api.openai.com/v1")
    self.assertEqual(config.timeout_seconds, 5)


if __name__ == "__main__":
  unittest.main()
