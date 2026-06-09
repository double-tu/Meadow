"""OpenAI-compatible chat completions provider."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from agent_kernel.domain.context import ModelContext


Transport = Callable[[str, dict[str, str], dict[str, Any], float], dict[str, Any]]


class OpenAICompatibleProvider:
  def __init__(
    self,
    api_key: str,
    base_url: str = "https://api.openai.com/v1",
    timeout_seconds: float = 60.0,
    transport: Transport | None = None,
  ) -> None:
    self._api_key = api_key
    self._base_url = base_url.rstrip("/")
    self._timeout_seconds = timeout_seconds
    self._transport = transport or _urllib_transport

  async def complete(self, model_ref: str, context: ModelContext) -> dict[str, object]:
    payload = {
      "model": model_ref,
      "messages": [_normalize_message(message) for message in context.messages],
    }
    if context.tool_schemas:
      payload["tools"] = context.tool_schemas
    headers = {
      "Authorization": f"Bearer {self._api_key}",
      "Content-Type": "application/json",
    }
    response = await asyncio.to_thread(
      self._transport,
      f"{self._base_url}/chat/completions",
      headers,
      payload,
      self._timeout_seconds,
    )
    return _parse_chat_completion_response(response)


def _normalize_message(message: dict[str, Any]) -> dict[str, Any]:
  role = str(message.get("role", "user"))
  content = message.get("content", "")
  normalized: dict[str, Any] = {"role": role}
  if content is None and role == "assistant" and isinstance(message.get("tool_calls"), list):
    normalized["content"] = None
  elif not isinstance(content, str):
    content = json.dumps(content, ensure_ascii=False, sort_keys=True)
    normalized["content"] = content
  else:
    normalized["content"] = content
  name = message.get("name")
  if role in {"user", "assistant", "tool"} and isinstance(name, str) and name:
    normalized["name"] = name
  if role == "assistant":
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
      normalized["tool_calls"] = tool_calls
  if role == "tool":
    tool_call_id = message.get("tool_call_id")
    if isinstance(tool_call_id, str) and tool_call_id:
      normalized["tool_call_id"] = tool_call_id
  return normalized


def _parse_chat_completion_response(response: dict[str, Any]) -> dict[str, object]:
  choices = response.get("choices")
  if not isinstance(choices, list) or not choices:
    raise ValueError("LLM response missing choices.")
  first = choices[0]
  if not isinstance(first, dict):
    raise ValueError("LLM response choice must be an object.")
  message = first.get("message")
  if not isinstance(message, dict):
    raise ValueError("LLM response choice missing message.")
  content = message.get("content", "")
  if content is None:
    content = ""
  elif not isinstance(content, str):
    content = json.dumps(content, ensure_ascii=False, sort_keys=True)
  parsed: dict[str, object] = {"content": content}
  tool_calls = message.get("tool_calls")
  if isinstance(tool_calls, list):
    parsed["tool_calls"] = tool_calls
  usage = response.get("usage")
  if isinstance(usage, dict):
    parsed["usage"] = usage
  return parsed


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
    raise RuntimeError(f"LLM HTTP error {exc.code}: {detail}") from exc
  except URLError as exc:
    raise RuntimeError(f"LLM network error: {exc.reason}") from exc
  return json.loads(raw)
