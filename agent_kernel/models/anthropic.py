"""Anthropic Messages API provider."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from agent_kernel.domain.context import ModelContext


AnthropicTransport = Callable[[str, dict[str, str], dict[str, Any], float], dict[str, Any]]


class AnthropicMessagesProvider:
  def __init__(
    self,
    api_key: str,
    base_url: str = "https://api.anthropic.com/v1",
    timeout_seconds: float = 60.0,
    transport: AnthropicTransport | None = None,
  ) -> None:
    self._api_key = api_key
    self._base_url = base_url.rstrip("/")
    self._timeout_seconds = timeout_seconds
    self._transport = transport or _urllib_transport

  async def complete(self, model_ref: str, context: ModelContext) -> dict[str, object]:
    system_parts: list[str] = []
    messages: list[dict[str, str]] = []
    for message in context.messages:
      role = str(message.get("role", "user"))
      content = _content_to_text(message.get("content", ""))
      if role == "system":
        system_parts.append(content)
      else:
        messages.append({"role": "assistant" if role == "assistant" else "user", "content": content})
    payload: dict[str, Any] = {
      "model": model_ref,
      "max_tokens": 4096,
      "messages": messages,
    }
    if system_parts:
      payload["system"] = "\n\n".join(system_parts)
    if context.tool_schemas:
      payload["tools"] = [_anthropic_tool_schema(tool) for tool in context.tool_schemas]
    response = await asyncio.to_thread(
      self._transport,
      f"{self._base_url}/messages",
      {
        "x-api-key": self._api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
      },
      payload,
      self._timeout_seconds,
    )
    return _parse_anthropic_response(response)


def _content_to_text(content: Any) -> str:
  if isinstance(content, str):
    return content
  return json.dumps(content, ensure_ascii=False, sort_keys=True)


def _anthropic_tool_schema(tool: dict[str, Any]) -> dict[str, Any]:
  function = tool.get("function") if isinstance(tool.get("function"), dict) else {}
  return {
    "name": str(function.get("name", "")),
    "description": str(function.get("description", "")),
    "input_schema": function.get("parameters") if isinstance(function.get("parameters"), dict) else {"type": "object"},
  }


def _parse_anthropic_response(response: dict[str, Any]) -> dict[str, object]:
  content = response.get("content")
  if not isinstance(content, list):
    raise ValueError("Anthropic response missing content blocks.")
  texts: list[str] = []
  tool_calls: list[dict[str, Any]] = []
  for block in content:
    if not isinstance(block, dict):
      continue
    if block.get("type") == "text" and isinstance(block.get("text"), str):
      texts.append(block["text"])
    if block.get("type") == "tool_use" and isinstance(block.get("name"), str):
      input_value = block.get("input")
      tool_calls.append({"name": block["name"], "input": input_value if isinstance(input_value, dict) else {}})
  result: dict[str, object] = {"content": "\n".join(texts)}
  if tool_calls:
    result["tool_calls"] = tool_calls
  usage = response.get("usage")
  if isinstance(usage, dict):
    result["usage"] = usage
  return result


def _urllib_transport(
  url: str,
  headers: dict[str, str],
  payload: dict[str, Any],
  timeout_seconds: float,
) -> dict[str, Any]:
  data = json.dumps(payload).encode("utf-8")
  request = Request(url, data=data, headers=headers, method="POST")
  try:
    with urlopen(request, timeout=timeout_seconds) as response:
      raw = response.read().decode("utf-8")
  except HTTPError as exc:
    detail = exc.read().decode("utf-8", errors="replace")
    raise RuntimeError(f"Anthropic HTTP error {exc.code}: {detail}") from exc
  except URLError as exc:
    raise RuntimeError(f"Anthropic network error: {exc.reason}") from exc
  return json.loads(raw)
