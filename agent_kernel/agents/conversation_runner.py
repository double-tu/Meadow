"""Conversational continuous execution runner.

The runner is a host-facing application service for tasks that are solved by
dialogue, exploration, and atomic capability calls. It is deliberately separate
from the durable workflow runtime: hosts can use it directly for daily
conversational work, or wrap it inside a workflow node later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Literal

from agent_kernel.capabilities.atomic import AtomicToolCatalog
from agent_kernel.capabilities.runtime import CapabilityCallContext, CapabilityRuntime
from agent_kernel.domain.base import new_id
from agent_kernel.domain.context import ContextAssemblyRequest, ModelContext
from agent_kernel.domain.events import RuntimeEvent, RuntimeEventType
from agent_kernel.domain.serialization import to_json
from agent_kernel.domain.skill import SkillCard
from agent_kernel.models.gateway import ModelGateway
from agent_kernel.policy.engine import PolicyDecisionType


RunnerStatus = Literal["completed", "max_turns_exceeded", "awaiting_approval", "waiting_for_user", "failed"]


@dataclass(slots=True)
class ContinuousRunnerConfig:
  provider_name: str = "mock"
  model_ref: str = "mock"
  max_turns: int = 12


@dataclass(slots=True)
class ContinuousToolCallRecord:
  name: str
  capability_id: str
  input: dict[str, Any]
  ok: bool
  output: dict[str, Any] = field(default_factory=dict)
  error: dict[str, Any] | None = None
  requires_approval: bool = False


@dataclass(slots=True)
class ContinuousRunnerResult:
  run_id: str
  status: RunnerStatus
  turns: int
  output: dict[str, Any] = field(default_factory=dict)
  tool_calls: list[ContinuousToolCallRecord] = field(default_factory=list)
  pending: dict[str, Any] | None = None


class ContinuousAgentRunner:
  """Runs model/tool turns until completion, approval, user input, or budget."""

  def __init__(
    self,
    *,
    uow_factory,
    model_gateway: ModelGateway,
    capability_runtime: CapabilityRuntime,
    tool_catalog: AtomicToolCatalog,
    context_assembler=None,
    context_manager=None,
    skills: list[SkillCard] | None = None,
    system_instructions: str | None = None,
  ) -> None:
    self._uow_factory = uow_factory
    self._model_gateway = model_gateway
    self._capability_runtime = capability_runtime
    self._tool_catalog = tool_catalog
    self._context_assembler = context_assembler
    self._context_manager = context_manager
    self._skills = skills or []
    self._system_instructions = system_instructions or _DEFAULT_SYSTEM_INSTRUCTIONS

  async def run(
    self,
    *,
    user_message: str,
    history_messages: list[dict[str, Any]] | None = None,
    run_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    scope: str | None = None,
    config: ContinuousRunnerConfig | None = None,
  ) -> ContinuousRunnerResult:
    config = config or ContinuousRunnerConfig()
    run_id = run_id or new_id("conv_run")
    scope = scope or run_id
    messages: list[dict[str, Any]] = self._initial_messages(user_message, history_messages or [])
    all_tool_calls: list[ContinuousToolCallRecord] = []
    self._append_event(
      RuntimeEvent(
        event_type=RuntimeEventType.RUN_CREATED,
        run_id=run_id,
        agent_id=agent_id,
        task_id=task_id,
        payload={"runner": "continuous_agent", "scope": scope},
      )
    )

    for turn in range(1, config.max_turns + 1):
      context = self._build_context(run_id, scope, config.model_ref, messages)
      model_result = await self._model_gateway.complete(config.provider_name, config.model_ref, context)
      model_result = self._normalize_model_result(model_result)
      tool_calls = self._extract_tool_calls(model_result)
      if not tool_calls:
        output = self._finish_output(model_result)
        self._append_turn_event(run_id, agent_id, task_id, turn, output, [])
        return ContinuousRunnerResult(
          run_id=run_id,
          status="completed",
          turns=turn,
          output=output,
          tool_calls=all_tool_calls,
        )

      turn_records: list[ContinuousToolCallRecord] = []
      tool_messages: list[dict[str, Any]] = []
      for raw_call in tool_calls:
        call = self._tool_catalog.normalize_call(
          raw_call["name"],
          raw_call["input"],
          run_id=run_id,
          scope=scope,
        )
        outcome = await self._capability_runtime.call(
          call.capability_id,
          call.input,
          CapabilityCallContext(
            run_id=run_id,
            agent_id=agent_id,
            task_id=task_id,
            idempotency_key=f"{run_id}:{turn}:{len(all_tool_calls) + len(turn_records)}:{call.capability_id}",
          ),
        )
        record = ContinuousToolCallRecord(
          name=call.display_name,
          capability_id=call.capability_id,
          input=call.input,
          ok=bool(outcome.result and outcome.result.ok),
          output=outcome.result.output if outcome.result is not None else {},
          error=outcome.result.error if outcome.result is not None else None,
          requires_approval=outcome.requires_approval,
        )
        turn_records.append(record)
        if outcome.result is not None:
          for event in outcome.result.events:
            self._append_event(event)
          if outcome.result.metadata.get("interrupt") == "user_input":
            all_tool_calls.extend(turn_records)
            pending = outcome.result.output
            self._append_turn_event(run_id, agent_id, task_id, turn, pending, turn_records)
            return ContinuousRunnerResult(
              run_id=run_id,
              status="waiting_for_user",
              turns=turn,
              output={},
              tool_calls=all_tool_calls,
              pending=pending,
            )
        if outcome.decision.type is PolicyDecisionType.REQUIRE_APPROVAL:
          all_tool_calls.extend(turn_records)
          pending = {"capability_id": call.capability_id, "reason": outcome.decision.reason}
          self._append_turn_event(run_id, agent_id, task_id, turn, pending, turn_records)
          return ContinuousRunnerResult(
            run_id=run_id,
            status="awaiting_approval",
            turns=turn,
            output={},
            tool_calls=all_tool_calls,
            pending=pending,
          )
        tool_messages.append(
          {
            "tool_name": call.display_name,
            "capability_id": call.capability_id,
            "ok": record.ok,
            "output": _compact_for_json(record.output, max_bytes=6000),
            "error": _compact_for_json(record.error, max_bytes=2000),
          }
        )
      all_tool_calls.extend(turn_records)
      self._append_turn_event(
        run_id,
        agent_id,
        task_id,
        turn,
        {"tool_results": _compact_for_json(tool_messages, max_bytes=5000)},
        turn_records,
      )
      messages = [
        *_normalize_history_messages(history_messages or []),
        {"role": "user", "content": user_message},
        {
          "role": "user",
          "content": {
            "type": "tool_results",
            "original_user_goal": user_message,
            "turn": turn,
            "results": tool_messages,
            "instruction": (
              "Continue working on original_user_goal. If the goal is not completed yet, call the next required tool. "
              "Do not finish by only summarizing intermediate inspection results unless they fully satisfy the original goal."
            ),
          },
        }
      ]
      if self._context_assembler is None:
        messages = [
          {"role": "system", "content": self._system_instructions},
          *self._skill_messages(),
          *messages,
        ]

    return ContinuousRunnerResult(
      run_id=run_id,
      status="max_turns_exceeded",
      turns=config.max_turns,
      output=_fallback_output_from_tool_calls(all_tool_calls),
      tool_calls=all_tool_calls,
    )

  def _skill_messages(self) -> list[dict[str, Any]]:
    if not self._skills:
      return []
    return [
      {
        "role": "system",
        "content": {
          "type": "selected_skills",
          "skills": [
            {
              "skill_id": skill.skill_id,
              "name": skill.name,
              "description": skill.description,
              "when_to_use": skill.when_to_use,
              "instructions": skill.instructions,
              "recommended_tools": skill.recommended_tools,
              "recommended_workflows": skill.recommended_workflows,
              "constraints": skill.constraints,
              "failure_modes": skill.failure_modes,
            }
            for skill in self._skills
          ],
        },
      }
    ]

  def _initial_messages(
    self,
    user_message: str,
    history_messages: list[dict[str, Any]],
  ) -> list[dict[str, Any]]:
    base_messages = [
      *_normalize_history_messages(history_messages),
      {"role": "user", "content": user_message},
    ]
    if self._context_assembler is not None:
      return base_messages
    return [
      {"role": "system", "content": self._system_instructions},
      *self._skill_messages(),
      *base_messages,
    ]

  def _build_context(
    self,
    run_id: str,
    scope: str,
    model_ref: str,
    messages: list[dict[str, Any]],
  ) -> ModelContext:
    tool_schemas = self._tool_catalog.tool_schemas()
    if self._context_assembler is not None:
      return self._context_assembler.assemble(
        ContextAssemblyRequest(
          run_id=run_id,
          scope=scope,
          model_ref=model_ref,
          messages=messages,
          system_instructions=self._system_instructions,
          skills=self._skills,
          tool_schemas=tool_schemas,
          max_tokens=4096,
        )
      ).model_context
    if self._context_manager is None:
      return ModelContext(messages=messages, tool_schemas=tool_schemas)
    context = self._context_manager.build(
      run_id=run_id,
      scope=scope,
      model_ref=model_ref,
      messages=messages,
      available_tools=tool_schemas,
    )
    context.tool_schemas = tool_schemas
    return context

  @staticmethod
  def _normalize_model_result(result: dict[str, object]) -> dict[str, Any]:
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
  def _extract_tool_calls(cls, result: dict[str, Any]) -> list[dict[str, Any]]:
    raw_calls = result.get("tool_calls")
    if isinstance(raw_calls, list):
      calls = []
      for raw in raw_calls:
        parsed = cls._parse_tool_call(raw)
        if parsed is not None:
          calls.append(parsed)
      return calls
    command = result.get("command")
    if isinstance(command, dict) and command.get("type") == "tool":
      name = command.get("name") or command.get("target")
      payload = command.get("input") or command.get("payload") or {}
      if isinstance(name, str) and isinstance(payload, dict):
        return [{"name": name, "input": payload}]
    return []

  @staticmethod
  def _parse_tool_call(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
      return None
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
    return {"name": name, "input": payload}

  @staticmethod
  def _finish_output(result: dict[str, Any]) -> dict[str, Any]:
    output = result.get("output", result.get("content", result))
    return output if isinstance(output, dict) else {"value": output}

  def _append_turn_event(
    self,
    run_id: str,
    agent_id: str | None,
    task_id: str | None,
    turn: int,
    output: dict[str, Any],
    tool_calls: list[ContinuousToolCallRecord],
  ) -> None:
    self._append_event(
      RuntimeEvent(
        event_type=RuntimeEventType.AGENT_TURN_COMPLETED,
        run_id=run_id,
        agent_id=agent_id,
        task_id=task_id,
        payload={
          "runner": "continuous_agent",
          "turn": turn,
          "output": _compact_for_json(output, max_bytes=5000),
          "tool_calls": [
            {
              "name": call.name,
              "capability_id": call.capability_id,
              "ok": call.ok,
              "requires_approval": call.requires_approval,
            }
            for call in tool_calls
          ],
        },
      )
    )

  def _append_event(self, event: RuntimeEvent) -> None:
    with self._uow_factory() as uow:
      uow.events.append(event)


_DEFAULT_SYSTEM_INSTRUCTIONS = (
  "你是 Meadow 日常 Agent。你必须基于用户目标、上下文、记忆和 Skills 自主决定下一步。"
  "需要实时信息、浏览器/桌面/移动控制、文件、代码执行、MCP、Workflow、子代理或任务分配时，"
  "优先调用可用工具，不要只说明自己可以做。工具结果会回灌给你继续推理。"
  "如果工具失败，必须基于失败结果继续尝试其他可用工具，或如实说明失败原因；不能编造工具没有返回的信息。"
  "如果任务完成，返回最终中文回答；如果缺少关键信息，调用用户输入能力。"
)


def _normalize_history_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
  normalized: list[dict[str, Any]] = []
  for message in messages:
    role = message.get("role")
    if role not in {"user", "assistant"}:
      continue
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
      continue
    normalized.append({"role": role, "content": content})
  return normalized


def _compact_for_json(value: Any, *, max_bytes: int) -> Any:
  if value is None:
    return None
  try:
    encoded = to_json(value).encode("utf-8")
  except TypeError:
    value = str(value)
    encoded = to_json(value).encode("utf-8")
  if len(encoded) <= max_bytes:
    return value
  if isinstance(value, str):
    return _truncate_text(value, max_bytes=max_bytes)
  if isinstance(value, dict):
    compact: dict[str, Any] = {
      "_truncated": True,
      "_original_bytes": len(encoded),
    }
    remaining = max(512, max_bytes - 256)
    for key, item in value.items():
      if key in {"artifact_refs", "body_summary", "error", "status", "ok", "url", "title", "targets"}:
        compact[key] = _compact_for_json(item, max_bytes=min(remaining, 2000))
        continue
      if key in {"body", "content", "text", "result"}:
        compact[key] = _compact_for_json(item, max_bytes=min(remaining, 3000))
        continue
      if len(to_json(compact).encode("utf-8")) >= max_bytes - 512:
        compact["_omitted_after_key"] = str(key)
        break
      compact[key] = _compact_for_json(item, max_bytes=min(remaining, 3000))
    return compact
  if isinstance(value, list):
    items = []
    for item in value[:8]:
      items.append(_compact_for_json(item, max_bytes=max(512, max_bytes // 4)))
      if len(to_json(items).encode("utf-8")) >= max_bytes - 512:
        break
    return {
      "_truncated": True,
      "_original_bytes": len(encoded),
      "_original_count": len(value),
      "items": items,
    }
  return {
    "_truncated": True,
    "_original_bytes": len(encoded),
    "preview": _truncate_text(str(value), max_bytes=max_bytes - 128),
  }


def _truncate_text(value: str, *, max_bytes: int) -> str:
  encoded = value.encode("utf-8")
  if len(encoded) <= max_bytes:
    return value
  budget = max(0, max_bytes - 96)
  preview = encoded[:budget].decode("utf-8", errors="ignore")
  return f"{preview}\n...[truncated {len(encoded) - budget} bytes]"


def _fallback_output_from_tool_calls(tool_calls: list[ContinuousToolCallRecord]) -> dict[str, Any]:
  if not tool_calls:
    return {
      "content": "日常 Agent 达到最大执行轮次，但没有完成任何工具调用。请重试或把目标拆得更具体。",
    }
  feed_titles: list[str] = []
  successful_urls: list[str] = []
  failed_tools: list[str] = []
  for call in tool_calls:
    if call.ok:
      url = call.output.get("url")
      if isinstance(url, str) and url:
        successful_urls.append(url)
      body_summary = call.output.get("body_summary")
      if isinstance(body_summary, dict):
        titles = body_summary.get("feed_titles")
        if isinstance(titles, list):
          feed_titles.extend(str(title) for title in titles if title)
    elif call.error:
      error_type = call.error.get("type", "unknown_error")
      failed_tools.append(f"{call.name}: {error_type}")
  if feed_titles:
    lines = ["已获取到页面数据，但执行轮次已用完。根据已返回的数据，看到的推荐内容包括："]
    lines.extend(f"- {title}" for title in _unique_strings(feed_titles)[:12])
    if failed_tools:
      lines.append("")
      lines.append("部分浏览器操作失败：" + "；".join(failed_tools[-3:]))
    return {"content": "\n".join(lines)}
  summary = [
    f"日常 Agent 达到最大执行轮次，已执行 {len(tool_calls)} 次工具调用。",
    f"成功 {sum(1 for call in tool_calls if call.ok)} 次，失败 {sum(1 for call in tool_calls if not call.ok)} 次。",
  ]
  if successful_urls:
    summary.append("已成功请求：" + "、".join(_unique_strings(successful_urls)[-3:]))
  if failed_tools:
    summary.append("最近失败：" + "；".join(failed_tools[-3:]))
  return {"content": "\n".join(summary)}


def _unique_strings(values: list[str]) -> list[str]:
  seen: set[str] = set()
  result: list[str] = []
  for value in values:
    if value in seen:
      continue
    seen.add(value)
    result.append(value)
  return result
