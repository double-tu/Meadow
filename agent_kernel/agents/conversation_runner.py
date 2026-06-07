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
from agent_kernel.domain.context import ModelContext
from agent_kernel.domain.events import RuntimeEvent, RuntimeEventType
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
    context_manager=None,
  ) -> None:
    self._uow_factory = uow_factory
    self._model_gateway = model_gateway
    self._capability_runtime = capability_runtime
    self._tool_catalog = tool_catalog
    self._context_manager = context_manager

  async def run(
    self,
    *,
    user_message: str,
    run_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    scope: str | None = None,
    config: ContinuousRunnerConfig | None = None,
  ) -> ContinuousRunnerResult:
    config = config or ContinuousRunnerConfig()
    run_id = run_id or new_id("conv_run")
    scope = scope or run_id
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]
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
            "output": record.output,
            "error": record.error,
          }
        )
      all_tool_calls.extend(turn_records)
      self._append_turn_event(run_id, agent_id, task_id, turn, {"tool_results": tool_messages}, turn_records)
      messages = [
        {
          "role": "user",
          "content": {
            "type": "tool_results",
            "turn": turn,
            "results": tool_messages,
            "instruction": "Use these results to continue, call another tool, or finish.",
          },
        }
      ]

    return ContinuousRunnerResult(
      run_id=run_id,
      status="max_turns_exceeded",
      turns=config.max_turns,
      output={},
      tool_calls=all_tool_calls,
    )

  def _build_context(
    self,
    run_id: str,
    scope: str,
    model_ref: str,
    messages: list[dict[str, Any]],
  ) -> ModelContext:
    tool_schemas = self._tool_catalog.tool_schemas()
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
          "output": output,
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
