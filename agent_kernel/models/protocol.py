"""Model context and tool-call protocol adaptation.

The rest of Meadow should not care whether a provider returns native tool
calls, JSON content, or text-form tool blocks. This module keeps that
compatibility layer small, deterministic, and testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any

from agent_kernel.domain.context import ModelContext


@dataclass(slots=True)
class ModelProtocolIssue:
  issue_type: str
  message: str
  raw: str | None = None

  def to_dict(self) -> dict[str, Any]:
    data = {"type": self.issue_type, "message": self.message}
    if self.raw is not None:
      data["raw"] = self.raw
    return data


@dataclass(slots=True)
class ToolProtocolAdaptation:
  result: dict[str, Any]
  issues: list[ModelProtocolIssue] = field(default_factory=list)


class ModelContextSanitizer:
  """Normalize messages before they reach provider adapters."""

  def sanitize(self, context: ModelContext) -> ModelContext:
    messages = self._sanitize_messages(context.messages)
    return ModelContext(
      messages=messages,
      tool_schemas=list(context.tool_schemas),
      attachments=list(context.attachments),
      memory_refs=list(context.memory_refs),
      omitted_candidates=list(context.omitted_candidates),
      token_budget_ledger=dict(context.token_budget_ledger),
      inclusion_rationale=dict(context.inclusion_rationale),
    )

  def _sanitize_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    system_messages: list[dict[str, Any]] = []
    conversation: list[dict[str, Any]] = []
    for raw in messages:
      sanitized = _sanitize_message(raw)
      role = str(sanitized["role"])
      if role == "system":
        system_messages.append(sanitized)
      else:
        conversation.append(sanitized)
    while conversation and conversation[0]["role"] in {"assistant", "tool"}:
      conversation.pop(0)
    merged: list[dict[str, Any]] = []
    for message in conversation:
      if (
        merged
        and message["role"] == merged[-1]["role"]
        and message["role"] != "tool"
        and set(message) == {"role", "content"}
        and set(merged[-1]) == {"role", "content"}
        and isinstance(message["content"], str)
        and isinstance(merged[-1]["content"], str)
      ):
        merged[-1]["content"] = _merge_content(merged[-1]["content"], message["content"])
      else:
        merged.append(message)
    return [*system_messages, *merged]


class ModelToolProtocolAdapter:
  """Adapt provider output into Meadow's normalized model-result shape."""

  def adapt(self, result: dict[str, object]) -> ToolProtocolAdaptation:
    normalized = self.normalize_result(result)
    issues: list[ModelProtocolIssue] = []
    content = _result_content_text(normalized)
    parsed_calls, cleaned_content, parse_issues = self._parse_text_tool_calls(content)
    issues.extend(parse_issues)
    native_calls = self.extract_tool_calls(normalized)
    if parsed_calls:
      native_calls.extend(parsed_calls)
      _set_result_content_text(normalized, cleaned_content)
    if native_calls:
      normalized["tool_calls"] = native_calls
    if issues:
      normalized["protocol_errors"] = [issue.to_dict() for issue in issues]
    return ToolProtocolAdaptation(result=normalized, issues=issues)

  @staticmethod
  def normalize_result(result: dict[str, object]) -> dict[str, Any]:
    if any(key in result for key in ("finish", "output", "tool_calls", "command")):
      return dict(result)
    content = result.get("content")
    if not isinstance(content, str):
      return dict(result)
    try:
      parsed = json.loads(content)
    except json.JSONDecodeError:
      return {"finish": True, "output": {"content": content}}
    return parsed if isinstance(parsed, dict) else {"finish": True, "output": {"content": content}}

  @classmethod
  def extract_tool_calls(cls, result: dict[str, Any]) -> list[dict[str, Any]]:
    raw_calls = result.get("tool_calls")
    calls: list[dict[str, Any]] = []
    if isinstance(raw_calls, list):
      for raw in raw_calls:
        parsed = cls.parse_tool_call(raw)
        if parsed is not None:
          calls.append(parsed)
    command = result.get("command")
    if isinstance(command, dict) and command.get("type") == "tool":
      name = command.get("name") or command.get("target")
      payload = command.get("input") or command.get("payload") or {}
      if isinstance(name, str) and isinstance(payload, dict):
        calls.append({"name": name, "input": payload})
    return calls

  @staticmethod
  def parse_tool_call(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
      return None
    call_id = raw.get("id") or raw.get("tool_call_id")
    name = raw.get("name")
    payload = raw.get("input", raw.get("arguments", {}))
    function = raw.get("function")
    if isinstance(function, dict):
      name = function.get("name", name)
      payload = function.get("arguments", payload)
    if isinstance(payload, str):
      try:
        payload = json.loads(payload)
      except json.JSONDecodeError:
        payload = {"value": payload}
    if not isinstance(name, str) or not isinstance(payload, dict):
      return None
    parsed = {"name": name, "input": payload}
    if isinstance(call_id, str) and call_id:
      parsed["id"] = call_id
    return parsed

  def _parse_text_tool_calls(self, content: str) -> tuple[list[dict[str, Any]], str, list[ModelProtocolIssue]]:
    if not content:
      return [], content, []
    calls: list[dict[str, Any]] = []
    issues: list[ModelProtocolIssue] = []
    cleaned = content
    for match in reversed(list(_TOOL_BLOCK_RE.finditer(content))):
      raw = match.group(1).strip().strip("`").strip()
      parsed, issue = _parse_tool_json(raw)
      if parsed is not None:
        calls[:0] = parsed
        cleaned = cleaned[: match.start()] + cleaned[match.end() :]
      elif issue is not None:
        issues.append(issue)
    if not calls:
      array_calls, array_start, array_issue = _parse_tool_json_array_suffix(content)
      if array_calls:
        calls.extend(array_calls)
        cleaned = content[:array_start].strip()
      elif array_issue is not None:
        issues.append(array_issue)
    return calls, cleaned.strip(), issues


_TOOL_BLOCK_RE = re.compile(r"<(?:tool_use|tool_call)>([\s\S]{2,}?)</(?:tool_use|tool_call)>", re.IGNORECASE)


def _merge_content(left: Any, right: Any) -> Any:
  if isinstance(left, str) and isinstance(right, str):
    return left + "\n" + right
  return [left, right]


def _sanitize_message(raw: dict[str, Any]) -> dict[str, Any]:
  role = str(raw.get("role") or "user").lower()
  if role not in {"system", "user", "assistant", "tool"}:
    role = "user"
  content = raw.get("content", "")
  if content is None and role != "assistant":
    content = ""
  sanitized: dict[str, Any] = {"role": role, "content": content}
  name = raw.get("name")
  if role in {"user", "assistant"} and isinstance(name, str) and name:
    sanitized["name"] = name
  if role == "assistant":
    tool_calls = raw.get("tool_calls")
    if isinstance(tool_calls, list):
      normalized_calls = []
      for call in tool_calls:
        if isinstance(call, dict):
          normalized_calls.append(dict(call))
      if normalized_calls:
        sanitized["tool_calls"] = normalized_calls
  if role == "tool":
    tool_call_id = raw.get("tool_call_id")
    if isinstance(tool_call_id, str) and tool_call_id:
      sanitized["tool_call_id"] = tool_call_id
    name = raw.get("name")
    if isinstance(name, str) and name:
      sanitized["name"] = name
  return sanitized


def _result_content_text(result: dict[str, Any]) -> str:
  output = result.get("output", result.get("content"))
  if isinstance(output, str):
    return output
  if isinstance(output, dict):
    value = output.get("content")
    return value if isinstance(value, str) else ""
  return ""


def _set_result_content_text(result: dict[str, Any], content: str) -> None:
  if "output" in result and isinstance(result["output"], dict):
    result["output"]["content"] = content
  elif "output" in result and isinstance(result["output"], str):
    result["output"] = {"content": content}
  else:
    result["content"] = content


def _parse_tool_json(raw: str) -> tuple[list[dict[str, Any]] | None, ModelProtocolIssue | None]:
  try:
    parsed = json.loads(raw)
  except json.JSONDecodeError as exc:
    return None, ModelProtocolIssue("tool_json_parse_error", str(exc), raw[:500])
  raw_items = parsed if isinstance(parsed, list) else [parsed]
  calls: list[dict[str, Any]] = []
  for item in raw_items:
    call = _tool_call_from_text_payload(item)
    if call is not None:
      calls.append(call)
  if not calls:
    return None, ModelProtocolIssue("tool_json_missing_name", "Tool JSON did not contain a usable tool name.", raw[:500])
  return calls, None


def _parse_tool_json_array_suffix(content: str) -> tuple[list[dict[str, Any]], int, ModelProtocolIssue | None]:
  start = min((idx for idx in [content.find('[{"type":"tool_use"'), content.find('[{"type": "tool_use"')] if idx >= 0), default=-1)
  if start < 0:
    return [], -1, None
  raw = content[start:].strip()
  calls, issue = _parse_tool_json(raw)
  return calls or [], start, issue


def _tool_call_from_text_payload(payload: object) -> dict[str, Any] | None:
  if not isinstance(payload, dict):
    return None
  name = payload.get("name") or payload.get("tool") or payload.get("function")
  args = payload.get("arguments", payload.get("args", payload.get("input", payload.get("parameters", {}))))
  if args is payload:
    args = {}
  if isinstance(args, str):
    try:
      args = json.loads(args)
    except json.JSONDecodeError:
      args = {"value": args}
  if not isinstance(name, str) or not isinstance(args, dict):
    return None
  return {"name": name, "input": args}
