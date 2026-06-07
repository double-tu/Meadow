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


def _normalize_message(message: dict[str, Any]) -> dict[str, str]:
  role = str(message.get("role", "user"))
  content = message.get("content", "")
  if not isinstance(content, str):
    content = json.dumps(content, ensure_ascii=False, sort_keys=True)
  return {"role": role, "content": content}


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
  if not isinstance(content, str):
    content = json.dumps(content, ensure_ascii=False, sort_keys=True)
  parsed: dict[str, object] = {"content": content}
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
