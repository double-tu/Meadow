"""Model-visible execution recovery hooks for continuous agent loops."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from typing import Any, Literal, Protocol

from agent_kernel.domain.serialization import to_json


HookSeverity = Literal["info", "warning", "blocking", "terminal"]


@dataclass(slots=True)
class ExecutionTransition:
  run_id: str
  turn: int
  reason: str
  original_goal: str
  metadata: dict[str, Any] = field(default_factory=dict)

  def to_dict(self) -> dict[str, Any]:
    return {
      "run_id": self.run_id,
      "turn": self.turn,
      "reason": self.reason,
      "original_goal": self.original_goal,
      "metadata": self.metadata,
    }


class ToolOutcomeView(Protocol):
  name: str
  capability_id: str
  input: dict[str, Any]
  ok: bool
  output: dict[str, Any]
  error: dict[str, Any] | None
  requires_approval: bool


@dataclass(slots=True)
class ProgressHookResult:
  reason: str
  severity: HookSeverity
  message: str
  repair_hint: str
  metadata: dict[str, Any] = field(default_factory=dict)
  model_visible: bool = True

  def to_dict(self) -> dict[str, Any]:
    return {
      "reason": self.reason,
      "severity": self.severity,
      "message": self.message,
      "repair_hint": self.repair_hint,
      "metadata": self.metadata,
      "model_visible": self.model_visible,
    }


class AgentProgressHook(Protocol):
  def after_tool_results(
    self,
    *,
    transition: ExecutionTransition,
    turn_records: list[ToolOutcomeView],
    all_records: list[ToolOutcomeView],
    action_history: list[str],
  ) -> list[ProgressHookResult]:
    ...

  def before_final_answer(
    self,
    *,
    transition: ExecutionTransition,
    model_output: dict[str, Any],
    all_records: list[ToolOutcomeView],
  ) -> list[ProgressHookResult]:
    ...


class NoProgressHook:
  """Detect repeated low-information actions without encoding task-specific flows."""

  def __init__(self, *, warning_threshold: int = 2, blocking_threshold: int = 3) -> None:
    self._warning_threshold = warning_threshold
    self._blocking_threshold = blocking_threshold
    self._signature_counts: dict[str, int] = {}
    self._low_info_counts: dict[str, int] = {}
    self._emitted_blocking: set[str] = set()

  def after_tool_results(
    self,
    *,
    transition: ExecutionTransition,
    turn_records: list[ToolOutcomeView],
    all_records: list[ToolOutcomeView],
    action_history: list[str],
  ) -> list[ProgressHookResult]:
    results: list[ProgressHookResult] = []
    for record in turn_records:
      signature = _tool_signature(record)
      self._signature_counts[signature] = self._signature_counts.get(signature, 0) + 1
      signature_count = self._signature_counts[signature]
      low_info_key = _low_information_key(record)
      low_info_count = 0
      if low_info_key is not None:
        self._low_info_counts[low_info_key] = self._low_info_counts.get(low_info_key, 0) + 1
        low_info_count = self._low_info_counts[low_info_key]
      count = max(signature_count, low_info_count)
      if count < self._warning_threshold:
        continue
      severity: HookSeverity = "blocking" if count >= self._blocking_threshold else "warning"
      reason = "no_progress_repeated_low_information" if low_info_key is not None else "no_progress_repeated_tool_call"
      identity = f"{reason}:{low_info_key or signature}"
      if severity == "blocking" and identity in self._emitted_blocking:
        continue
      if severity == "blocking":
        self._emitted_blocking.add(identity)
      detail = _record_progress_detail(record)
      results.append(
        ProgressHookResult(
          reason=reason,
          severity=severity,
          message=(
            f"{record.name} has repeated without enough new evidence. "
            f"repeat_count={count}; detail={detail}"
          ),
          repair_hint=(
            "Do not repeat the same low-information action. Switch strategy: inspect a specific target/page, "
            "change input/source/tool, open the relevant Skill/SOP, read the research_ledger/action_history, "
            "open candidate sources, use browser_execute_js for precise DOM extraction, read prior events/artifacts, "
            "delegate a subtask, or ask the user for the concrete missing information. If blocked, return a blocker report with evidence."
          ),
          metadata={
            "turn": transition.turn,
            "tool_name": record.name,
            "capability_id": record.capability_id,
            "repeat_count": count,
            "low_information": low_info_key is not None,
            "detail": detail,
          },
        )
      )
    return results

  def before_final_answer(
    self,
    *,
    transition: ExecutionTransition,
    model_output: dict[str, Any],
    all_records: list[ToolOutcomeView],
  ) -> list[ProgressHookResult]:
    return []


class StopHook:
  """Block structurally invalid final answers and make the correction model-visible."""

  def before_final_answer(
    self,
    *,
    transition: ExecutionTransition,
    model_output: dict[str, Any],
    all_records: list[ToolOutcomeView],
  ) -> list[ProgressHookResult]:
    text = _output_text(model_output)
    if not text.strip():
      return []
    normalized = " ".join(text.split()).lower()
    invalid_markers = (
      "已完成运行，但没有返回可展示内容",
      "已完成但没有返回可展示内容",
      "没有返回可展示内容",
      "completed but no content",
      "no displayable content",
    )
    if any(marker.lower() in normalized for marker in invalid_markers):
      return [
        ProgressHookResult(
          reason="stop_hook_invalid_empty_final",
          severity="blocking",
          message="The model attempted to finish with an empty or non-actionable final answer.",
          repair_hint=(
            "Produce a concrete final answer from available tool evidence, or explain the exact blocker, "
            "recent failures, and next action. Do not say the task completed with no content."
          ),
          metadata={"turn": transition.turn, "tool_count": len(all_records)},
        )
      ]
    if all_records and _looks_like_unexplained_failure(text, all_records):
      return [
        ProgressHookResult(
          reason="stop_hook_missing_failure_diagnosis",
          severity="blocking",
          message="The final answer did not explain recent tool failures or blockers.",
          repair_hint=(
            "Summarize what was attempted, what evidence was obtained, which tools failed, "
            "and what the next recoverable action should be."
          ),
          metadata={"turn": transition.turn, "failed_tool_count": sum(1 for record in all_records if not record.ok)},
        )
      ]
    return []

  def after_tool_results(
    self,
    *,
    transition: ExecutionTransition,
    turn_records: list[ToolOutcomeView],
    all_records: list[ToolOutcomeView],
    action_history: list[str],
  ) -> list[ProgressHookResult]:
    return []


class ExecutionDiagnosticSynthesizer:
  """Build terminal diagnostics for max-turn or failed runs."""

  def synthesize(
    self,
    *,
    original_goal: str,
    status: str,
    turns: int,
    tool_calls: list[ToolOutcomeView],
    hook_results: list[ProgressHookResult],
  ) -> dict[str, Any]:
    failures = _failure_summaries(tool_calls)
    evidence = _evidence_summaries(tool_calls)
    no_progress_causes = [
      result.message
      for result in hook_results
      if result.reason.startswith("no_progress") and result.severity in {"warning", "blocking", "terminal"}
    ]
    next_steps = _next_steps(failures=failures, evidence=evidence, no_progress_causes=no_progress_causes)
    return {
      "diagnostics": {
        "type": "execution_terminal_diagnostic",
        "status": status,
        "original_goal": original_goal,
        "turns": turns,
        "tool_count": len(tool_calls),
        "success_count": sum(1 for call in tool_calls if call.ok),
        "failure_count": sum(1 for call in tool_calls if not call.ok),
        "completed_actions": _completed_action_summaries(tool_calls),
        "evidence": evidence,
        "failures": failures,
        "no_progress_causes": no_progress_causes[-8:],
        "next_steps": next_steps,
      }
    }


def hook_results_to_model_messages(results: list[ProgressHookResult]) -> list[dict[str, Any]]:
  messages: list[dict[str, Any]] = []
  for result in results:
    if not result.model_visible:
      continue
    messages.append(
      {
        "type": "execution_hook",
        "hook": result.reason,
        "severity": result.severity,
        "message": result.message,
        "repair_hint": result.repair_hint,
        "metadata": result.metadata,
      }
    )
  return messages


def terminal_diagnostic_content(base_content: str, diagnostics: dict[str, Any]) -> str:
  payload = diagnostics.get("diagnostics") if isinstance(diagnostics, dict) else None
  if not isinstance(payload, dict):
    return base_content
  lines = [base_content.strip()] if base_content.strip() else []
  no_progress = payload.get("no_progress_causes")
  failures = payload.get("failures")
  next_steps = payload.get("next_steps")
  if no_progress:
    lines.append("")
    lines.append("未完成原因：" + "；".join(str(item) for item in no_progress[-3:]))
  elif payload.get("status") in {"max_turns_exceeded", "failed"}:
    lines.append("")
    lines.append(f"未完成原因：{payload.get('status')}。")
  if failures:
    rendered = []
    for item in failures[-3:]:
      if not isinstance(item, dict):
        continue
      rendered.append(f"{item.get('tool_name')}: {item.get('error_type')}")
    if rendered:
      lines.append("最近失败：" + "；".join(rendered))
  if next_steps:
    lines.append("建议下一步：" + "；".join(str(item) for item in next_steps[:3]))
  return "\n".join(lines).strip()


def _tool_signature(record: ToolOutcomeView) -> str:
  return to_json(
    {
      "capability_id": record.capability_id,
      "input": _stable_input(record.input),
      "error_type": (record.error or {}).get("type") if not record.ok else None,
    }
  )


def _stable_input(value: Any) -> Any:
  if isinstance(value, dict):
    return {
      key: _stable_input(item)
      for key, item in sorted(value.items())
      if key not in {"run_id", "scope", "idempotency_key"}
    }
  if isinstance(value, list):
    return [_stable_input(item) for item in value]
  return value


def _low_information_key(record: ToolOutcomeView) -> str | None:
  if not record.ok:
    return f"failed:{record.capability_id}:{(record.error or {}).get('type', 'unknown')}"
  if record.capability_id.endswith(".skill.open") or record.name == "skill_open":
    skill_id = record.input.get("skill_id") or record.input.get("id")
    return f"skill_open:{skill_id}" if isinstance(skill_id, str) and skill_id else None
  if "browser.scan" in record.capability_id or record.name == "browser_scan":
    if bool(record.input.get("tabs_only")):
      return "browser_scan:tabs_only:" + _hash_value(record.output.get("targets", []))
    page = record.output.get("page")
    if isinstance(page, dict):
      fingerprint = _page_fingerprint(page)
      if fingerprint:
        return f"browser_scan:page:{fingerprint}"
    if "targets" in record.output and "page" not in record.output:
      return "browser_scan:targets_only:" + _hash_value(record.output.get("targets", []))
  if "browser.execute_js" in record.capability_id or record.name == "browser_execute_js":
    payload = _browser_js_payload(record.output)
    if _is_empty_payload(payload):
      target = record.input.get("target_id") or record.output.get("target_id")
      return f"browser_execute_js:empty:{target}:{_hash_value(_stable_input(record.input))}"
  return None


def _page_fingerprint(page: dict[str, Any]) -> str | None:
  parts = []
  for key in ("url", "title"):
    value = page.get(key)
    if isinstance(value, str) and value.strip():
      parts.append(value.strip())
  text = page.get("text")
  if isinstance(text, str) and text.strip():
    parts.append(" ".join(text.split())[:500])
  for key in ("feed_titles", "visible_cards", "search_results"):
    value = page.get(key)
    if value:
      parts.append(to_json(value)[:500])
  if not parts:
    return None
  return _hash_value(parts)


def _browser_js_payload(output: dict[str, Any]) -> Any:
  for key in ("js_return", "data"):
    if key in output:
      return output.get(key)
  result = output.get("result")
  if isinstance(result, dict):
    for key in ("js_return", "data"):
      if key in result:
        return result.get(key)
  return result


def _is_empty_payload(value: Any) -> bool:
  if value is None:
    return True
  if isinstance(value, str):
    return not value.strip() or value.strip() in {"[]", "{}", "null"}
  if isinstance(value, (list, dict)):
    return not value
  return False


def _hash_value(value: Any) -> str:
  text = to_json(value)
  return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _record_progress_detail(record: ToolOutcomeView) -> str:
  if not record.ok:
    return str((record.error or {}).get("type", "unknown_error"))
  if record.name == "browser_scan" or "browser.scan" in record.capability_id:
    page = record.output.get("page")
    if isinstance(page, dict):
      return str(page.get("url") or page.get("title") or "page")
    targets = record.output.get("targets")
    if isinstance(targets, list):
      return f"{len(targets)} browser targets"
  if record.name == "skill_open":
    return str(record.input.get("skill_id") or "skill")
  return record.capability_id


def _output_text(output: dict[str, Any]) -> str:
  parts: list[str] = []
  for key in ("content", "summary", "value", "text"):
    value = output.get(key)
    if isinstance(value, str):
      parts.append(value)
  return "\n".join(parts)


def _looks_like_unexplained_failure(text: str, records: list[ToolOutcomeView]) -> bool:
  if not any(not record.ok for record in records):
    return False
  if any(token in text for token in ("失败", "错误", "阻塞", "无法", "未能", "原因", "failed", "error", "blocker")):
    return False
  compact = "".join(text.split())
  return len(compact) < 80


def _completed_action_summaries(records: list[ToolOutcomeView]) -> list[str]:
  items: list[str] = []
  for record in records:
    if not record.ok:
      continue
    detail = _record_progress_detail(record)
    items.append(f"{record.name}: {detail}")
  return _unique(items)[-12:]


def _failure_summaries(records: list[ToolOutcomeView]) -> list[dict[str, Any]]:
  failures: list[dict[str, Any]] = []
  for record in records:
    if record.ok:
      continue
    error = record.error or {}
    failures.append(
      {
        "tool_name": record.name,
        "capability_id": record.capability_id,
        "error_type": error.get("type", "unknown_error"),
        "message": error.get("message"),
      }
    )
  return failures[-12:]


def _evidence_summaries(records: list[ToolOutcomeView]) -> list[dict[str, Any]]:
  evidence: list[dict[str, Any]] = []
  for record in records:
    if not record.ok:
      continue
    page = record.output.get("page")
    if isinstance(page, dict):
      item: dict[str, Any] = {"type": "browser_page", "tool_name": record.name}
      for key in ("title", "url"):
        value = page.get(key)
        if isinstance(value, str) and value.strip():
          item[key] = value.strip()
      text = page.get("text")
      if isinstance(text, str) and text.strip():
        item["observation"] = " ".join(text.split())[:300]
      for key in ("feed_titles", "visible_cards", "search_results"):
        value = page.get(key)
        if value:
          item[key] = value[:5] if isinstance(value, list) else value
      if len(item) > 2:
        evidence.append(item)
        continue
    url = record.output.get("url")
    if isinstance(url, str) and url.strip():
      evidence.append({"type": "url", "tool_name": record.name, "url": url.strip()})
      continue
    artifact_refs = record.output.get("artifact_refs")
    if artifact_refs:
      evidence.append({"type": "artifact_refs", "tool_name": record.name, "artifact_refs": artifact_refs})
  return evidence[-12:]


def _next_steps(
  *,
  failures: list[dict[str, Any]],
  evidence: list[dict[str, Any]],
  no_progress_causes: list[str],
) -> list[str]:
  steps: list[str] = []
  if no_progress_causes:
    steps.append("继续时先更换策略, 不要重复同一低信息工具调用。")
  if failures:
    steps.append("根据最近失败类型切换工具、输入或来源, 必要时请求用户授权/登录/补充信息。")
  if evidence:
    steps.append("基于已获得的页面/工具证据先生成阶段性结论, 再决定是否继续深挖。")
  if not steps:
    steps.append("重新开始时先打开相关 Skill/SOP, 明确证据需求后再调用工具。")
  return steps


def _unique(values: list[str]) -> list[str]:
  seen: set[str] = set()
  result: list[str] = []
  for value in values:
    if value in seen:
      continue
    seen.add(value)
    result.append(value)
  return result
