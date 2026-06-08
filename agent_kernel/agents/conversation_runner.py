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
    last_repetition_key: str | None = None
    consecutive_repeated_tool_calls = 0
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
        output = self._finish_output(model_result, all_tool_calls)
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
        repetition_key = self._tool_repetition_key(call.capability_id, call.input)
        if repetition_key == last_repetition_key:
          consecutive_repeated_tool_calls += 1
        else:
          last_repetition_key = repetition_key
          consecutive_repeated_tool_calls = 1
        if consecutive_repeated_tool_calls > 3:
          warning_record = ContinuousToolCallRecord(
            name=call.display_name,
            capability_id=call.capability_id,
            input=call.input,
            ok=False,
            error={
              "type": "repeated_tool_call_guard",
              "message": "The same tool call was repeated too many times without changing input.",
            },
          )
          turn_records.append(warning_record)
          all_tool_calls.extend(turn_records)
          output = _fallback_output_from_tool_calls(all_tool_calls)
          self._append_turn_event(run_id, agent_id, task_id, turn, output, turn_records)
          return ContinuousRunnerResult(
            run_id=run_id,
            status="max_turns_exceeded",
            turns=turn,
            output=output,
            tool_calls=all_tool_calls,
          )
        outcome = await self._capability_runtime.call(
          call.capability_id,
          call.input,
          CapabilityCallContext(
            run_id=run_id,
            agent_id=agent_id,
            task_id=task_id,
            scope=scope,
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
              "Do not finish by only summarizing intermediate inspection results unless they fully satisfy the original goal. "
              "Follow the relevant Skill/SOP instructions when a selected or active Skill applies."
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
  def _finish_output(result: dict[str, Any], tool_calls: list[ContinuousToolCallRecord] | None = None) -> dict[str, Any]:
    output = result.get("output", result.get("content", result))
    normalized = output if isinstance(output, dict) else {"value": output}
    if _has_displayable_output(normalized):
      return normalized
    if tool_calls:
      return _fallback_output_from_tool_calls(tool_calls)
    return normalized

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

  @staticmethod
  def _tool_repetition_key(capability_id: str, input: dict[str, Any]) -> str:
    return to_json({"capability_id": capability_id, "input": _stable_tool_input(input)})


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


def _stable_tool_input(value: Any) -> Any:
  if isinstance(value, dict):
    return {
      key: _stable_tool_input(item)
      for key, item in sorted(value.items())
      if key not in {"run_id", "scope"}
    }
  if isinstance(value, list):
    return [_stable_tool_input(item) for item in value]
  return value


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
  browser_observations: list[dict[str, Any]] = []
  successful_urls: list[str] = []
  failed_tools: list[str] = []
  access_issues: list[str] = []
  created_workbenches: list[dict[str, Any]] = []
  delegations: list[dict[str, Any]] = []
  for call in tool_calls:
    if call.ok:
      workbench = _extract_workbench_summary(call.output)
      if workbench is not None:
        created_workbenches.append(workbench)
      delegations.extend(_extract_delegation_summaries(call.output))
      url = call.output.get("url")
      if isinstance(url, str) and url:
        successful_urls.append(url)
      access_issue = _describe_access_issue(call)
      if access_issue is not None:
        access_issues.append(access_issue)
      body_summary = call.output.get("body_summary")
      if isinstance(body_summary, dict):
        titles = body_summary.get("feed_titles")
        if isinstance(titles, list):
          feed_titles.extend(str(title) for title in titles if title)
      browser_observation = _extract_browser_observation(call.output)
      if browser_observation is not None:
        browser_observations.append(browser_observation)
    elif call.error:
      error_type = call.error.get("type", "unknown_error")
      failed_tools.append(f"{call.name}: {error_type}")
  if created_workbenches:
    lines = ["已创建/推进协作工作台，后续可以在工作台继续查看、暂停、重试或追加消息："]
    for item in created_workbenches[-5:]:
      label = item.get("title") or item.get("workbench_id") or "未命名工作台"
      kind = item.get("kind") or "workbench"
      status = item.get("status") or "unknown"
      lines.append(f"- {label}（{kind}，状态：{status}，ID：{item.get('workbench_id') or 'unknown'}）")
    if delegations:
      lines.append("")
      lines.append(f"已关联 {len(delegations)} 个子 Agent/CLI 委派任务。")
    if access_issues:
      lines.append("")
      lines.append("访问受限：" + "；".join(_unique_strings(access_issues)[-3:]))
    if failed_tools:
      lines.append("")
      lines.append("部分操作失败：" + "；".join(_humanize_tool_failures(failed_tools[-3:])))
    return {"content": "\n".join(lines)}
  if delegations:
    lines = [f"已启动/查询 {len(delegations)} 个子 Agent 委派任务："]
    for item in delegations[-8:]:
      label = item.get("task") or item.get("task_id") or "子任务"
      status = item.get("status") or "unknown"
      lines.append(f"- {label}（状态：{status}，ID：{item.get('task_id') or 'unknown'}）")
    if failed_tools:
      lines.append("")
      lines.append("部分操作失败：" + "；".join(_humanize_tool_failures(failed_tools[-3:])))
    return {"content": "\n".join(lines)}
  if feed_titles:
    lines = ["已获取到页面数据，但执行轮次已用完。根据已返回的数据，看到的推荐内容包括："]
    lines.extend(f"- {title}" for title in _unique_strings(feed_titles)[:12])
    if access_issues:
      lines.append("")
      lines.append("访问受限：" + "；".join(_unique_strings(access_issues)[-3:]))
    if failed_tools:
      lines.append("")
      lines.append("部分浏览器操作失败：" + "；".join(_humanize_tool_failures(failed_tools[-3:])))
    return {"content": "\n".join(lines)}
  if browser_observations:
    lines = ["已通过浏览器读取到页面内容，但执行轮次已用完。根据已返回的页面观察："]
    for item in browser_observations[-3:]:
      title = item.get("title")
      url = item.get("url")
      if title or url:
        lines.append(f"- 页面：{title or url}")
      cards = item.get("visible_cards")
      if isinstance(cards, list) and cards:
        lines.extend(f"  - {card}" for card in _unique_strings([str(card) for card in cards])[:6])
      search_results = item.get("search_results")
      if isinstance(search_results, list) and search_results:
        lines.append("  - 搜索结果候选（尚需继续打开结果页核验）：")
        for result in search_results[:5]:
          if not isinstance(result, dict):
            continue
          title = str(result.get("title") or "").strip()
          href = str(result.get("href") or "").strip()
          snippet = str(result.get("snippet") or "").strip()
          if title and href:
            line = f"    - {title}: {href}"
            if snippet:
              line += f"；摘要：{_compact_browser_text(snippet)[:240]}"
            lines.append(line)
      links = item.get("links")
      if not search_results and isinstance(links, list) and links:
        lines.append("  - 候选链接：")
        for link in links[:5]:
          if not isinstance(link, dict):
            continue
          text = str(link.get("text") or "").strip()
          href = str(link.get("href") or "").strip()
          if text and href:
            lines.append(f"    - {text}: {href}")
      text = item.get("text")
      if isinstance(text, str) and text.strip():
        lines.append("  - 摘要：" + _compact_browser_text(text))
    if access_issues:
      lines.append("")
      lines.append("访问受限：" + "；".join(_unique_strings(access_issues)[-3:]))
    if failed_tools:
      lines.append("")
      lines.append("部分操作失败：" + "；".join(_humanize_tool_failures(failed_tools[-3:])))
    return {"content": "\n".join(lines)}
  summary = [
    f"日常 Agent 达到最大执行轮次，已执行 {len(tool_calls)} 次工具调用。",
    f"成功 {sum(1 for call in tool_calls if call.ok)} 次，失败 {sum(1 for call in tool_calls if not call.ok)} 次。",
  ]
  if successful_urls:
    summary.append("已成功请求：" + "、".join(_unique_strings(successful_urls)[-3:]))
  if access_issues:
    summary.append("访问受限：" + "；".join(_unique_strings(access_issues)[-3:]))
  if failed_tools:
    summary.append("最近失败：" + "；".join(_humanize_tool_failures(failed_tools[-3:])))
  return {"content": "\n".join(summary)}


def _has_displayable_output(output: dict[str, Any]) -> bool:
  for key in ("content", "summary", "value"):
    value = output.get(key)
    if isinstance(value, str) and value.strip():
      return True
  return bool(output) and output != {"content": ""} and output != {"value": ""} and output != {"output": ""}


def _extract_workbench_summary(output: dict[str, Any]) -> dict[str, Any] | None:
  workbench_wrapper = output.get("workbench")
  if not isinstance(workbench_wrapper, dict):
    return None
  workbench = workbench_wrapper.get("workbench")
  if not isinstance(workbench, dict):
    return None
  return {
    "workbench_id": workbench.get("workbench_id"),
    "title": workbench.get("title"),
    "kind": workbench.get("kind"),
    "status": workbench.get("status"),
  }


def _extract_delegation_summaries(output: dict[str, Any]) -> list[dict[str, Any]]:
  raw_items: list[Any] = []
  delegation = output.get("delegation")
  if isinstance(delegation, dict):
    raw_items.append(delegation)
  delegations = output.get("delegations")
  if isinstance(delegations, list):
    raw_items.extend(delegations)
  workbench_wrapper = output.get("workbench")
  if isinstance(workbench_wrapper, dict):
    workbench_delegations = workbench_wrapper.get("delegations")
    if isinstance(workbench_delegations, list):
      raw_items.extend(workbench_delegations)
  summaries: list[dict[str, Any]] = []
  for item in raw_items:
    if not isinstance(item, dict):
      continue
    summaries.append(
      {
        "task_id": item.get("task_id"),
        "task": item.get("task"),
        "status": item.get("status"),
      }
    )
  return summaries


def _extract_browser_observation(output: dict[str, Any]) -> dict[str, Any] | None:
  page = output.get("page")
  if not isinstance(page, dict):
    return None
  observation: dict[str, Any] = {}
  for key in ("title", "url", "text"):
    value = page.get(key)
    if isinstance(value, str) and value.strip():
      observation[key] = value.strip()
  for key in ("feed_titles", "visible_cards"):
    value = page.get(key)
    if isinstance(value, list):
      items = [str(item).strip() for item in value if str(item).strip()]
      if items:
        observation[key] = items
  links = page.get("links")
  if isinstance(links, list):
    normalized_links = []
    for item in links:
      if not isinstance(item, dict):
        continue
      text = item.get("text")
      href = item.get("href")
      if isinstance(text, str) and text.strip() and isinstance(href, str) and href.strip():
        normalized_links.append({"text": text.strip(), "href": href.strip()})
    if normalized_links:
      observation["links"] = normalized_links[:10]
  search_results = page.get("search_results")
  if isinstance(search_results, list):
    normalized_results = []
    for item in search_results:
      if not isinstance(item, dict):
        continue
      title = item.get("title")
      href = item.get("href")
      snippet = item.get("snippet")
      if isinstance(title, str) and title.strip() and isinstance(href, str) and href.strip():
        result = {"title": title.strip(), "href": href.strip()}
        if isinstance(snippet, str) and snippet.strip():
          result["snippet"] = snippet.strip()
        normalized_results.append(result)
    if normalized_results:
      observation["search_results"] = normalized_results[:8]
  return observation or None


def _compact_browser_text(text: str) -> str:
  compact = " ".join(text.split())
  if len(compact) <= 600:
    return compact
  return compact[:600] + "...[truncated]"


def _unique_strings(values: list[str]) -> list[str]:
  seen: set[str] = set()
  result: list[str] = []
  for value in values:
    if value in seen:
      continue
    seen.add(value)
    result.append(value)
  return result


def _describe_access_issue(call: ContinuousToolCallRecord) -> str | None:
  issue = call.output.get("access_issue")
  if not isinstance(issue, dict):
    return None
  issue_type = issue.get("type")
  url = call.output.get("url")
  label = str(url) if isinstance(url, str) and url else call.name
  if issue_type in {"anti_spider_challenge", "captcha_or_challenge", "anti_bot_rate_limit"}:
    return f"{label} 返回反爬/验证码页面，HTTP 结果不能当作有效搜索内容"
  message = issue.get("message")
  return f"{label} 访问受限：{message}" if isinstance(message, str) and message else f"{label} 访问受限"


def _humanize_tool_failures(failures: list[str]) -> list[str]:
  return [_humanize_tool_failure(item) for item in failures]


def _humanize_tool_failure(value: str) -> str:
  translations = {
    "adapter_not_configured": "适配器未配置",
    "network_error": "网络请求失败",
    "http_error": "HTTP 请求失败",
    "workbench_error": "工作台操作失败",
    "delegation_error": "子 Agent 委派失败",
    "invalid_input": "工具参数无效",
  }
  if ": " not in value:
    return translations.get(value, value)
  name, error_type = value.split(": ", 1)
  return f"{name}: {translations.get(error_type, error_type)}"
