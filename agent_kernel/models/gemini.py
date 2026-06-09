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
      if role == "system":
        content = _content_to_text(message.get("content", ""))
        system_parts.append({"text": content})
      else:
        payload["contents"].append(_gemini_message(message))
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


def _gemini_message(message: dict[str, Any]) -> dict[str, Any]:
  role = str(message.get("role", "user"))
  if role == "assistant":
    parts = _gemini_assistant_parts(message)
    return {"role": "model", "parts": parts if parts else [{"text": ""}]}
  if role == "tool":
    name = message.get("name")
    return {
      "role": "user",
      "parts": [
        {
          "functionResponse": {
            "name": name if isinstance(name, str) and name else "",
            "response": _gemini_tool_response(message.get("content", "")),
          }
        }
      ],
    }
  return {"role": "user", "parts": [{"text": _content_to_text(message.get("content", ""))}]}


def _gemini_assistant_parts(message: dict[str, Any]) -> list[dict[str, Any]]:
  parts: list[dict[str, Any]] = []
  content = _content_to_text(message.get("content", ""))
  if content:
    parts.append({"text": content})
  tool_calls = message.get("tool_calls")
  if isinstance(tool_calls, list):
    for raw_call in tool_calls:
      call = _parse_tool_call(raw_call)
      if call is not None:
        parts.append({"functionCall": {"name": call["name"], "args": call["input"]}})
  return parts


def _parse_tool_call(raw_call: Any) -> dict[str, Any] | None:
  if not isinstance(raw_call, dict):
    return None
  name = raw_call.get("name")
  payload = raw_call.get("input", raw_call.get("arguments", {}))
  function = raw_call.get("function")
  if isinstance(function, dict):
    name = function.get("name", name)
    payload = function.get("arguments", payload)
  if isinstance(payload, str):
    try:
      payload = json.loads(payload)
    except json.JSONDecodeError:
      payload = {"value": payload}
  if not isinstance(name, str) or not name or not isinstance(payload, dict):
    return None
  return {"name": name, "input": payload}


def _gemini_tool_response(content: Any) -> dict[str, Any]:
  if isinstance(content, dict):
    return content
  if isinstance(content, str):
    try:
      parsed = json.loads(content)
    except json.JSONDecodeError:
      return {"content": content}
    if isinstance(parsed, dict):
      return parsed
    return {"content": parsed}
  return {"content": content}


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
