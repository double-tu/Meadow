"""Google Gemini generateContent provider."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from agent_kernel.domain.context import ModelContext


GeminiTransport = Callable[[str, dict[str, str], dict[str, Any], float], dict[str, Any]]


class GeminiProvider:
  def __init__(
    self,
    api_key: str,
    base_url: str = "https://generativelanguage.googleapis.com/v1beta",
    timeout_seconds: float = 60.0,
    transport: GeminiTransport | None = None,
  ) -> None:
    self._api_key = api_key
    self._base_url = base_url.rstrip("/")
    self._timeout_seconds = timeout_seconds
    self._transport = transport or _urllib_transport

  async def complete(self, model_ref: str, context: ModelContext) -> dict[str, object]:
    payload: dict[str, Any] = {
      "contents": [],
    }
    system_parts: list[dict[str, str]] = []
    for message in context.messages:
      role = str(message.get("role", "user"))
      content = _content_to_text(message.get("content", ""))
      if role == "system":
        system_parts.append({"text": content})
      else:
        payload["contents"].append(
          {
            "role": "model" if role == "assistant" else "user",
            "parts": [{"text": content}],
          }
        )
    if system_parts:
      payload["systemInstruction"] = {"parts": system_parts}
    if context.tool_schemas:
      payload["tools"] = [{"functionDeclarations": [_gemini_tool_schema(tool) for tool in context.tool_schemas]}]

    query = urlencode({"key": self._api_key})
    url = f"{self._base_url}/models/{quote(model_ref, safe='')}:generateContent?{query}"
    response = await asyncio.to_thread(
      self._transport,
      url,
      {"Content-Type": "application/json"},
      payload,
      self._timeout_seconds,
    )
    return _parse_gemini_response(response)


def _content_to_text(content: Any) -> str:
  if isinstance(content, str):
    return content
  return json.dumps(content, ensure_ascii=False, sort_keys=True)


def _gemini_tool_schema(tool: dict[str, Any]) -> dict[str, Any]:
  function = tool.get("function") if isinstance(tool.get("function"), dict) else {}
  return {
    "name": str(function.get("name", "")),
    "description": str(function.get("description", "")),
    "parameters": function.get("parameters") if isinstance(function.get("parameters"), dict) else {"type": "object"},
  }


def _parse_gemini_response(response: dict[str, Any]) -> dict[str, object]:
  candidates = response.get("candidates")
  if not isinstance(candidates, list) or not candidates:
    raise ValueError("Gemini response missing candidates.")
  content = candidates[0].get("content") if isinstance(candidates[0], dict) else None
  parts = content.get("parts") if isinstance(content, dict) else None
  if not isinstance(parts, list):
    raise ValueError("Gemini response missing content parts.")
  texts: list[str] = []
  tool_calls: list[dict[str, Any]] = []
  for part in parts:
    if not isinstance(part, dict):
      continue
    text = part.get("text")
    if isinstance(text, str):
      texts.append(text)
    function_call = part.get("functionCall")
    if isinstance(function_call, dict) and isinstance(function_call.get("name"), str):
      args = function_call.get("args")
      tool_calls.append({"name": function_call["name"], "input": args if isinstance(args, dict) else {}})
  result: dict[str, object] = {"content": "\n".join(texts)}
  if tool_calls:
    result["tool_calls"] = tool_calls
  usage = response.get("usageMetadata")
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
    raise RuntimeError(f"Gemini HTTP error {exc.code}: {detail}") from exc
  except URLError as exc:
    raise RuntimeError(f"Gemini network error: {exc.reason}") from exc
  return json.loads(raw)
