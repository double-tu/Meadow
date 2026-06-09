"""Conversational continuous execution runner.

The runner is a host-facing application service for tasks that are solved by
dialogue, exploration, and atomic capability calls. It is deliberately separate
from the durable workflow runtime: hosts can use it directly for daily
conversational work, or wrap it inside a workflow node later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any, Callable, Literal

from agent_kernel.agents.execution_hooks import (
  ExecutionDiagnosticSynthesizer,
  ExecutionTransition,
  NoProgressHook,
  ProgressHookResult,
  StopHook,
  hook_results_to_model_messages,
  terminal_diagnostic_content,
)
from agent_kernel.capabilities.atomic import AtomicToolCatalog
from agent_kernel.capabilities.runtime import CapabilityCallContext, CapabilityRuntime
from agent_kernel.domain.base import new_id
from agent_kernel.domain.context import ContextAssemblyRequest, ModelContext
from agent_kernel.domain.events import RuntimeEvent, RuntimeEventType
from agent_kernel.domain.identifiers import ArtifactRef
from agent_kernel.domain.serialization import to_json
from agent_kernel.domain.skill import SkillCard
from agent_kernel.agents.goal_evidence import GoalEvidenceVerifier
from agent_kernel.agents.research_ledger import ResearchLedger
from agent_kernel.agents.run_anchor import RunAnchor
from agent_kernel.agents.tool_surface import SkillAwareToolSurfacePolicy, ToolSurfacePolicy, ToolSurfaceSelection
from agent_kernel.memory.facade import MemoryFacade
from agent_kernel.models.gateway import ModelGateway
from agent_kernel.models.protocol import ModelContextSanitizer, ModelToolProtocolAdapter
from agent_kernel.policy.engine import PolicyDecisionType


RunnerStatus = Literal["completed", "max_turns_exceeded", "awaiting_approval", "waiting_for_user", "failed", "cancelled"]


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
  model_tool_call_id: str | None = None


@dataclass(slots=True)
class ContinuousStepOutcome:
  """Normalized model-visible outcome for one executed capability call."""

  tool_name: str
  capability_id: str
  ok: bool
  model_visible_result: dict[str, Any] = field(default_factory=dict)
  error: dict[str, Any] | None = None
  repair_hint: str | None = None
  anchor_update: str | None = None

  def to_model_message(self) -> dict[str, Any]:
    payload = {
      "tool_name": self.tool_name,
      "capability_id": self.capability_id,
      "ok": self.ok,
      "output": self.model_visible_result,
      "error": self.error,
    }
    if self.repair_hint:
      payload["repair_hint"] = self.repair_hint
    if self.anchor_update:
      payload["anchor_update"] = self.anchor_update
    return payload


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
    tool_surface_policy: ToolSurfacePolicy | None = None,
    goal_evidence_verifier: GoalEvidenceVerifier | None = None,
    context_sanitizer: ModelContextSanitizer | None = None,
    tool_protocol_adapter: ModelToolProtocolAdapter | None = None,
    memory: MemoryFacade | None = None,
    system_instructions: str | None = None,
  ) -> None:
    self._uow_factory = uow_factory
    self._model_gateway = model_gateway
    self._capability_runtime = capability_runtime
    self._tool_catalog = tool_catalog
    self._context_assembler = context_assembler
    self._context_manager = context_manager
    self._skills = skills or []
    self._tool_surface_policy = tool_surface_policy or SkillAwareToolSurfacePolicy()
    self._goal_evidence_verifier = goal_evidence_verifier or GoalEvidenceVerifier()
    self._context_sanitizer = context_sanitizer or ModelContextSanitizer()
    self._tool_protocol_adapter = tool_protocol_adapter or ModelToolProtocolAdapter()
    self._memory = memory
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
    should_cancel: Callable[[], bool] | None = None,
  ) -> ContinuousRunnerResult:
    config = config or ContinuousRunnerConfig()
    run_id = run_id or new_id("conv_run")
    scope = scope or run_id
    messages: list[dict[str, Any]] = self._initial_messages(user_message, history_messages or [])
    run_anchor = RunAnchor.from_messages(
      original_user_goal=user_message,
      history_messages=history_messages or [],
    )
    research_ledger = ResearchLedger.from_goal(user_message)
    all_tool_calls: list[ContinuousToolCallRecord] = []
    action_history: list[str] = []
    all_hook_results: list[ProgressHookResult] = []
    finalization_instruction: str | None = None
    last_repetition_key: str | None = None
    consecutive_repeated_tool_calls = 0
    empty_model_result_count = 0
    stop_hook_block_count = 0
    progress_hooks = [NoProgressHook(), StopHook()]
    diagnostic_synthesizer = ExecutionDiagnosticSynthesizer()
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
      if should_cancel is not None and should_cancel():
        output = _cancelled_output(run_id=run_id, turn=turn)
        self._append_turn_event(
          run_id,
          agent_id,
          task_id,
          turn,
          output,
          [],
          transition=ExecutionTransition(
            run_id=run_id,
            turn=turn,
            reason="user_cancelled",
            original_goal=user_message,
          ),
        )
        return ContinuousRunnerResult(
          run_id=run_id,
          status="cancelled",
          turns=turn,
          output=output,
          tool_calls=all_tool_calls,
        )
      tool_surface = self._select_tool_surface(user_message, turn)
      if finalization_instruction is not None:
        tool_surface = ToolSurfaceSelection(
          skills=tool_surface.skills,
          tool_schemas=[],
          allowed_tool_names=[],
          rationale="goal_evidence_satisfied",
        )
      context = self._build_context(
        run_id,
        scope,
        config.model_ref,
        messages,
        tool_surface,
        run_anchor=run_anchor,
        turn=turn,
        action_history=action_history,
        research_ledger=research_ledger,
      )
      context = self._context_sanitizer.sanitize(context)
      adaptation = self._tool_protocol_adapter.adapt(
        await self._model_gateway.complete(config.provider_name, config.model_ref, context)
      )
      model_result = adaptation.result
      tool_calls = self._tool_protocol_adapter.extract_tool_calls(model_result)
      if not tool_calls:
        repair_output = _protocol_error_output(model_result, turn=turn) or _repairable_no_tool_output(model_result, turn=turn)
        if all_tool_calls and _is_blank_no_tool_diagnostic(repair_output):
          output = self._finish_output(model_result, all_tool_calls, turn=turn)
        else:
          output = repair_output or self._finish_output(model_result, all_tool_calls, turn=turn)
        if _is_empty_model_diagnostic(output):
          empty_model_result_count += 1
          self._append_turn_event(run_id, agent_id, task_id, turn, output, [])
          if empty_model_result_count >= 3:
            return ContinuousRunnerResult(
              run_id=run_id,
              status="failed",
              turns=turn,
              output=output,
              tool_calls=all_tool_calls,
            )
          messages.append(
            {
              "role": "user",
              "content": {
                "type": "model_repair",
                "original_user_goal": user_message,
                "turn": turn,
                "empty_response_count": empty_model_result_count,
                "diagnostics": output.get("diagnostics", {}),
                "instruction": (
                  str((output.get("diagnostics") or {}).get("instruction") or "[System] Blank response. Regenerate and continue the original task.")
                  + " "
                  "If tools are needed, call the next appropriate tool. If the task is impossible, "
                  "return a concise final answer explaining the concrete blocker and evidence. "
                  "Use working_memory_anchor to preserve continuity."
                ),
              },
            }
          )
          continue
        if not _is_empty_model_diagnostic(output):
          stop_results = []
          transition = ExecutionTransition(
            run_id=run_id,
            turn=turn,
            reason="final_answer",
            original_goal=user_message,
          )
          for hook in progress_hooks:
            stop_results.extend(
              hook.before_final_answer(
                transition=transition,
                model_output=output,
                all_records=all_tool_calls,
              )
            )
          blocking_stop_results = [result for result in stop_results if result.severity in {"blocking", "terminal"}]
          if blocking_stop_results and stop_hook_block_count < 2 and turn < config.max_turns:
            stop_hook_block_count += 1
            all_hook_results.extend(blocking_stop_results)
            self._append_turn_event(
              run_id,
              agent_id,
              task_id,
              turn,
              {"stop_hook_blocking": [result.to_dict() for result in blocking_stop_results]},
              [],
              transition=transition,
              hook_results=blocking_stop_results,
            )
            messages.append(
              {
                "role": "user",
                "content": {
                  "type": "model_repair",
                  "original_user_goal": user_message,
                  "turn": turn,
                  "diagnostics": {"type": "stop_hook_blocking"},
                  "execution_hooks": hook_results_to_model_messages(blocking_stop_results),
                  "instruction": (
                    "A stop hook blocked the previous final answer because it was not actionable enough. "
                    "Use available tool evidence to produce a concrete final answer, or explain the exact blocker, "
                    "recent failures, and next recoverable action. Do not return empty/no-content completion text."
                  ),
                },
              }
            )
            continue
          if blocking_stop_results:
            all_hook_results.extend(blocking_stop_results)
            terminal_output = _terminal_output_from_tool_calls(
              all_tool_calls,
              original_goal=user_message,
              status="failed",
              turns=turn,
              hook_results=all_hook_results,
              diagnostic_synthesizer=diagnostic_synthesizer,
            )
            self._append_turn_event(
              run_id,
              agent_id,
              task_id,
              turn,
              terminal_output,
              [],
              transition=transition,
              hook_results=blocking_stop_results,
            )
            return ContinuousRunnerResult(
              run_id=run_id,
              status="failed",
              turns=turn,
              output=terminal_output,
              tool_calls=all_tool_calls,
            )
        self._append_turn_event(run_id, agent_id, task_id, turn, output, [])
        run_anchor.record_turn(model_result=model_result, records=[])
        return ContinuousRunnerResult(
          run_id=run_id,
          status="completed",
          turns=turn,
          output=output,
          tool_calls=all_tool_calls,
        )

      turn_records: list[ContinuousToolCallRecord] = []
      empty_model_result_count = 0
      tool_messages: list[dict[str, Any]] = []
      tool_calls = _ensure_tool_call_ids(tool_calls, turn=turn)
      messages.append(_assistant_transcript_message(model_result, tool_calls))
      for raw_call in tool_calls:
        if should_cancel is not None and should_cancel():
          output = _cancelled_output(run_id=run_id, turn=turn)
          self._append_turn_event(
            run_id,
            agent_id,
            task_id,
            turn,
            output,
            turn_records,
            transition=ExecutionTransition(
              run_id=run_id,
              turn=turn,
              reason="user_cancelled",
              original_goal=user_message,
            ),
          )
          return ContinuousRunnerResult(
            run_id=run_id,
            status="cancelled",
            turns=turn,
            output=output,
            tool_calls=[*all_tool_calls, *turn_records],
          )
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
            model_tool_call_id=_tool_call_id(raw_call),
          )
          turn_records.append(warning_record)
          tool_message = ContinuousStepOutcome(
            tool_name=call.display_name,
            capability_id=call.capability_id,
            ok=False,
            error=warning_record.error,
            repair_hint=(
              "Repeated identical tool call was blocked. Inspect the actual state, change inputs, "
              "switch tool/source, open the relevant Skill/SOP, or ask the user with the concrete blocker."
            ),
            anchor_update=_anchor_update_from_record(warning_record),
          ).to_model_message()
          tool_messages.append(tool_message)
          messages.append(
            _tool_transcript_message(
              raw_call,
              tool_message,
              name=call.display_name,
            )
          )
          continue
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
          model_tool_call_id=_tool_call_id(raw_call),
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
        self._artifactize_large_tool_output(run_id=run_id, turn=turn, record=record)
        tool_message = _step_outcome_from_record(record).to_model_message()
        tool_messages.append(tool_message)
        messages.append(
          _tool_transcript_message(
            raw_call,
            tool_message,
            name=call.display_name,
          )
        )
      all_tool_calls.extend(turn_records)
      action_history.extend(_summarize_turn_records(turn_records, turn=turn))
      run_anchor.record_turn(model_result=model_result, records=turn_records)
      research_ledger.observe_records(turn_records, turn=turn)
      transition = ExecutionTransition(
        run_id=run_id,
        turn=turn,
        reason="tool_result_feedback",
        original_goal=user_message,
        metadata={"tool_count": len(turn_records)},
      )
      turn_hook_results: list[ProgressHookResult] = []
      for hook in progress_hooks:
        turn_hook_results.extend(
          hook.after_tool_results(
            transition=transition,
            turn_records=turn_records,
            all_records=all_tool_calls,
            action_history=action_history,
          )
        )
      if turn_hook_results:
        all_hook_results.extend(turn_hook_results)
        action_history.extend(_summarize_hook_results(turn_hook_results, turn=turn))
      self._persist_anchor_snapshot(
        scope=scope,
        run_id=run_id,
        turn=turn,
        run_anchor=run_anchor,
        action_history=action_history,
        research_ledger=research_ledger,
        hook_results=turn_hook_results,
        turn_records=turn_records,
      )
      execution_hook_messages = hook_results_to_model_messages(turn_hook_results)
      evidence = self._goal_evidence_verifier.evaluate(user_message, all_tool_calls)
      finalization_instruction = evidence.instruction if evidence.satisfied else None
      self._append_turn_event(
        run_id,
        agent_id,
        task_id,
        turn,
        {
          "tool_results": _compact_for_json(tool_messages, max_bytes=5000),
          "execution_hooks": _compact_for_json([result.to_dict() for result in turn_hook_results], max_bytes=3000),
        },
        turn_records,
        transition=transition,
        hook_results=turn_hook_results,
      )
      messages.append(
        {
          "role": "user",
          "content": {
            "type": "runtime_context",
            "original_user_goal": user_message,
            "turn": turn,
            "action_history": action_history[-12:],
            "research_ledger": research_ledger.to_model_payload(),
            "tool_result_count": len(tool_messages),
            "execution_hooks": execution_hook_messages,
            "instruction": (
              "Continue working on original_user_goal. If the goal is not completed yet, call the next required tool. "
              "Do not finish by only summarizing intermediate inspection results unless they fully satisfy the original goal. "
              "Use action_history to avoid repeating failed or already-completed steps. "
              "Use research_ledger to track visited sources, evidence, candidate links, and failures for research/browser tasks; "
              "open promising candidate sources and extract evidence before finalizing when the user asked for search or investigation. "
              "Use working_memory_anchor to preserve continuity across user replies and tool-result turns. "
              "Follow the relevant Skill/SOP instructions when a selected or active Skill applies. "
              "If execution_hooks are present, treat them as model-visible runtime repair instructions and do not repeat the blocked strategy. "
              + (
                finalization_instruction
                if finalization_instruction is not None
                else "When browser/page/search/feed observations fully satisfy the goal, stop tool use and provide the final answer."
              )
              + _progress_guard_instruction(turn)
            ),
          },
        }
      )
    return ContinuousRunnerResult(
      run_id=run_id,
      status="max_turns_exceeded",
      turns=config.max_turns,
      output=_terminal_output_from_tool_calls(
        all_tool_calls,
        original_goal=user_message,
        status="max_turns_exceeded",
        turns=config.max_turns,
        hook_results=all_hook_results,
        diagnostic_synthesizer=diagnostic_synthesizer,
      ),
      tool_calls=all_tool_calls,
    )

  def _select_tool_surface(self, user_message: str, turn: int) -> ToolSurfaceSelection:
    return self._tool_surface_policy.select(
      user_goal=user_message,
      skills=self._skills,
      tool_schemas=self._tool_catalog.tool_schemas(),
      turn=turn,
    )

  def _tool_surface_messages(self, selection: ToolSurfaceSelection) -> list[dict[str, Any]]:
    if not selection.allowed_tool_names and not selection.rationale:
      return []
    return [
      {
        "role": "system",
        "content": {
          "type": "tool_surface",
          "rationale": selection.rationale,
          "allowed_tools": selection.allowed_tool_names,
          "instruction": (
            "Use only these currently exposed tools. If allowed_tools is empty, produce the final answer only. "
            "If a relevant Skill/SOP is selected, follow it as the task procedure. "
            "When observations already satisfy the user goal, stop calling tools and provide the final answer."
          ),
        },
      }
    ]

  def _skill_messages(self, skills: list[SkillCard]) -> list[dict[str, Any]]:
    if not skills:
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
            for skill in skills
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
    return base_messages

  def _build_context(
    self,
    run_id: str,
    scope: str,
    model_ref: str,
    messages: list[dict[str, Any]],
    tool_surface: ToolSurfaceSelection,
    *,
    run_anchor: RunAnchor,
    turn: int,
    action_history: list[str],
    research_ledger: ResearchLedger,
  ) -> ModelContext:
    tool_schemas = tool_surface.tool_schemas
    research_payload = research_ledger.to_model_payload()
    anchored_messages = [
      run_anchor.render_message(current_turn=turn, action_history=action_history),
      *messages,
    ]
    if self._context_assembler is not None:
      metadata: dict[str, Any] = {}
      if research_payload is not None:
        metadata["working_memory"] = {
          "research_ledger": research_payload,
          "research_instructions": [
            "Search/result pages are candidate discovery, not verified evidence.",
            "Open promising candidate sources and extract evidence before finalizing research tasks.",
            "If progress stalls, switch source/tool/query, use browser_execute_js for precise DOM extraction, or report a concrete blocker.",
          ],
        }
      return self._context_assembler.assemble(
        ContextAssemblyRequest(
          run_id=run_id,
          scope=scope,
          model_ref=model_ref,
          messages=anchored_messages,
          system_instructions=self._system_instructions,
          skills=tool_surface.skills,
          tool_schemas=tool_schemas,
          max_tokens=4096,
          metadata=metadata,
        )
      ).model_context
    research_message = research_ledger.render_message(current_turn=turn)
    model_messages = [
      {"role": "system", "content": self._system_instructions},
      *self._tool_surface_messages(tool_surface),
      *self._skill_messages(tool_surface.skills),
      *([research_message] if research_message is not None else []),
      *anchored_messages,
    ]
    if self._context_manager is None:
      return ModelContext(messages=model_messages, tool_schemas=tool_schemas)
    context = self._context_manager.build(
      run_id=run_id,
      scope=scope,
      model_ref=model_ref,
      messages=model_messages,
      available_tools=tool_schemas,
    )
    context.tool_schemas = tool_schemas
    return context

  @staticmethod
  def _finish_output(
    result: dict[str, Any],
    tool_calls: list[ContinuousToolCallRecord] | None = None,
    *,
    turn: int | None = None,
  ) -> dict[str, Any]:
    output = result.get("output", result.get("content", result))
    normalized = output if isinstance(output, dict) else {"value": output}
    if _has_displayable_output(normalized):
      return normalized
    if tool_calls:
      return _fallback_output_from_tool_calls(tool_calls)
    return _diagnostic_output_for_empty_model_result(result, turn=turn)

  def _append_turn_event(
    self,
    run_id: str,
    agent_id: str | None,
    task_id: str | None,
    turn: int,
    output: dict[str, Any],
    tool_calls: list[ContinuousToolCallRecord],
    *,
    transition: ExecutionTransition | None = None,
    hook_results: list[ProgressHookResult] | None = None,
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
          "transition": transition.to_dict() if transition is not None else None,
          "output": _compact_for_json(output, max_bytes=5000),
          "hook_results": _compact_for_json(
            [result.to_dict() for result in hook_results or []],
            max_bytes=3000,
          ),
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

  def _persist_anchor_snapshot(
    self,
    *,
    scope: str,
    run_id: str,
    turn: int,
    run_anchor: RunAnchor,
    action_history: list[str],
    research_ledger: ResearchLedger,
    hook_results: list[ProgressHookResult],
    turn_records: list[ContinuousToolCallRecord],
  ) -> None:
    if self._memory is None:
      return
    content: dict[str, Any] = {
      "kind": "run_anchor_snapshot",
      "run_id": run_id,
      "turn": turn,
      "original_user_goal": run_anchor.original_user_goal,
      "history": run_anchor.history_lines[-12:],
      "action_history": action_history[-12:],
      "last_tool_outcomes": [
        {
          "name": record.name,
          "capability_id": record.capability_id,
          "ok": record.ok,
          "error_type": (record.error or {}).get("type") if record.error else None,
        }
        for record in turn_records[-8:]
      ],
    }
    research_payload = research_ledger.to_model_payload()
    if research_payload is not None:
      content["research_ledger"] = research_payload
    if hook_results:
      content["execution_hooks"] = [result.to_dict() for result in hook_results[-6:]]
    self._memory.write_working(
      scope,
      content,
      importance=0.96,
      created_by="continuous_agent_anchor",
    )

  def _artifactize_large_tool_output(
    self,
    *,
    run_id: str,
    turn: int,
    record: ContinuousToolCallRecord,
    max_inline_bytes: int = 8000,
  ) -> None:
    try:
      encoded = to_json(record.output).encode("utf-8")
    except (TypeError, ValueError):
      return
    if len(encoded) <= max_inline_bytes:
      return
    artifact = ArtifactRef(
      artifact_id=new_id("art_tool_output"),
      uri=f"memory://runs/{run_id}/tool-output/{turn}/{record.name}",
      media_type="application/json",
    )
    metadata = {
      "kind": "large_tool_output",
      "run_id": run_id,
      "turn": turn,
      "tool_name": record.name,
      "capability_id": record.capability_id,
      "content": to_json(record.output),
      "original_bytes": len(encoded),
    }
    with self._uow_factory() as uow:
      uow.artifacts.save(artifact, metadata)
    record.output = {
      "_artifactized": True,
      "artifact_id": artifact.artifact_id,
      "uri": artifact.uri,
      "media_type": artifact.media_type,
      "original_bytes": len(encoded),
      "preview": _compact_for_json(record.output, max_bytes=4000),
    }


_DEFAULT_SYSTEM_INSTRUCTIONS = (
  "你是 Meadow 日常 Agent。你必须基于用户目标、上下文、记忆和 Skills 自主决定下一步。"
  "需要实时信息、浏览器/桌面/移动控制、文件、代码执行、MCP、Workflow、子代理或任务分配时，"
  "优先调用可用工具，不要只说明自己可以做。工具结果会回灌给你继续推理。"
  "如果工具失败，必须基于失败结果继续尝试其他可用工具，或如实说明失败原因；不能编造工具没有返回的信息。"
  "对搜索/调研/浏览器任务，搜索结果页和 AI 概览只是候选线索；需要打开相关来源页、提取证据、交叉核验后再回答。"
  "如果连续工具调用没有新增证据，必须切换策略：换查询/来源/工具，使用 browser_execute_js 精确抽取，打开 Skill/SOP，委派子任务，或给出具体阻塞报告。"
  "如果任务完成，返回最终中文回答；如果缺少关键信息，调用用户输入能力。"
)


def _normalize_history_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
  normalized: list[dict[str, Any]] = []
  for message in messages:
    role = message.get("role")
    if role not in {"system", "user", "assistant", "tool"}:
      continue
    content = message.get("content")
    has_tool_calls = role == "assistant" and isinstance(message.get("tool_calls"), list)
    has_tool_result = role == "tool" and isinstance(message.get("tool_call_id"), str)
    if content is None and not has_tool_calls:
      continue
    if isinstance(content, str) and not content.strip() and not (has_tool_calls or has_tool_result):
      continue
    normalized_message = dict(message)
    normalized_message["role"] = role
    normalized.append(normalized_message)
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
    lines = ["已获取到页面数据，但执行轮次已用完。根据已返回的数据，页面条目/候选内容包括："]
    lines.extend(f"- {title}" for title in _unique_strings(feed_titles)[:12])
    if access_issues:
      lines.append("")
      lines.append("访问受限：" + "；".join(_unique_strings(access_issues)[-3:]))
    if failed_tools:
      lines.append("")
      lines.append("部分浏览器操作失败：" + "；".join(_humanize_tool_failures(failed_tools[-3:])))
    return {"content": "\n".join(lines)}
  if browser_observations:
    browser_feed_titles: list[str] = []
    for item in browser_observations:
      titles = item.get("feed_titles")
      if isinstance(titles, list):
        browser_feed_titles.extend(str(title) for title in titles if str(title).strip())
    if browser_feed_titles:
      lines = ["已通过浏览器获取到页面内容，执行轮次已用完。根据已返回的数据，页面条目/候选内容包括："]
      lines.extend(f"- {title}" for title in _unique_strings(browser_feed_titles)[:20])
      if access_issues:
        lines.append("")
        lines.append("访问受限：" + "；".join(_unique_strings(access_issues)[-3:]))
      if failed_tools:
        lines.append("")
        lines.append("部分浏览器操作失败：" + "；".join(_humanize_tool_failures(failed_tools[-3:])))
      return {"content": "\n".join(lines)}
    lines = ["已通过浏览器读取到页面内容，但执行轮次已用完。根据已返回的页面观察："]
    for item in browser_observations[-3:]:
      title = item.get("title")
      url = item.get("url")
      if title or url:
        lines.append(f"- 页面：{title or url}")
      titles = item.get("feed_titles")
      if isinstance(titles, list) and titles:
        lines.append("  - 页面条目/候选内容：")
        lines.extend(f"    - {feed_title}" for feed_title in _unique_strings([str(feed_title) for feed_title in titles])[:8])
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


def _terminal_output_from_tool_calls(
  tool_calls: list[ContinuousToolCallRecord],
  *,
  original_goal: str,
  status: RunnerStatus,
  turns: int,
  hook_results: list[ProgressHookResult],
  diagnostic_synthesizer: ExecutionDiagnosticSynthesizer,
) -> dict[str, Any]:
  fallback = _fallback_output_from_tool_calls(tool_calls)
  diagnostics = diagnostic_synthesizer.synthesize(
    original_goal=original_goal,
    status=status,
    turns=turns,
    tool_calls=tool_calls,
    hook_results=hook_results,
  )
  content = str(fallback.get("content") or "")
  if not content.strip():
    content = f"日常 Agent 未能完成任务：{original_goal}"
  fallback["content"] = terminal_diagnostic_content(content, diagnostics)
  fallback.update(diagnostics)
  return fallback


def _diagnostic_output_for_empty_model_result(result: dict[str, Any], *, turn: int | None = None) -> dict[str, Any]:
  markers = []
  for key in ("finish", "finish_reason", "stop_reason"):
    value = result.get(key)
    if value is not None:
      markers.append(f"{key}={value}")
  marker_text = "；".join(markers) if markers else "模型未提供 finish/stop 标记"
  turn_text = f"第 {turn} 轮" if turn is not None else "当前轮"
  return {
    "content": (
      f"日常 Agent 在{turn_text}没有返回可展示内容，也没有发起工具调用。\n"
      f"诊断：{marker_text}。\n"
      "这通常表示模型提前结束、输出为空，或工具面/Skill 指令没有让模型继续推进任务。请重试；"
      "如果再次出现，需要查看该 run 的 model call 与 agent.turn.completed 事件。"
    ),
    "diagnostics": {
      "type": "empty_model_result",
      "turn": turn,
      "model_result_keys": sorted(str(key) for key in result.keys()),
    },
  }


def _cancelled_output(*, run_id: str, turn: int) -> dict[str, Any]:
  return {
    "content": "任务已根据用户请求暂停。可以稍后重试或继续发新消息调整任务。",
    "diagnostics": {
      "type": "user_cancelled",
      "run_id": run_id,
      "turn": turn,
    },
  }


def _protocol_error_output(result: dict[str, Any], *, turn: int | None = None) -> dict[str, Any] | None:
  errors = result.get("protocol_errors")
  if not isinstance(errors, list) or not errors:
    return None
  turn_text = f"第 {turn} 轮" if turn is not None else "当前轮"
  return {
    "content": (
      f"日常 Agent 在{turn_text}输出了无法解析的工具调用协议。\n"
      "系统已把协议错误回灌给模型重新生成合法工具调用或给出明确阻塞原因。"
    ),
    "diagnostics": {
      "type": "empty_model_result",
      "reason": "tool_protocol_error",
      "turn": turn,
      "protocol_errors": errors[:3],
      "instruction": (
        "[System] The previous tool call protocol was invalid. Regenerate the tool call using valid JSON "
        "arguments for one of the exposed tools, or provide a concise final answer with the concrete blocker."
      ),
      "model_result_keys": sorted(str(key) for key in result.keys()),
    },
  }


def _repairable_no_tool_output(result: dict[str, Any], *, turn: int | None = None) -> dict[str, Any] | None:
  content = _model_result_text(result)
  reason: str | None = None
  instruction = "[System] Blank response. Regenerate and continue the original task."
  if not content.strip():
    return _diagnostic_output_for_empty_model_result(result, turn=turn)
  tail = content[-160:]
  finish_reason = str(result.get("finish_reason") or result.get("stop_reason") or "")
  if "[!!! 流异常中断" in tail or "!!!Error:" in tail:
    reason = "stream_interrupted"
    instruction = "[System] Incomplete response. Regenerate and continue with tool use or a concrete blocker."
  elif "max_tokens !!!]" in tail or finish_reason in {"length", "max_tokens"}:
    reason = "max_tokens_limit"
    instruction = "[System] max_tokens limit reached. Continue in smaller steps and use tools/artifacts for large content."
  elif _looks_like_unexecuted_large_code(content):
    reason = "large_code_without_tool"
    instruction = (
      "[System] The previous response mainly contained a large code block but did not call a tool. "
      "If code must be executed or written, call the appropriate tool. If it is only an explanation, "
      "answer with concise natural language and a clear final result."
    )
  if reason is None:
    return None
  turn_text = f"第 {turn} 轮" if turn is not None else "当前轮"
  return {
    "content": (
      f"日常 Agent 在{turn_text}没有发起工具调用，且输出需要修复。\n"
      f"诊断：{reason}。\n"
      "系统已把修复指令回灌给模型继续执行。"
    ),
    "diagnostics": {
      "type": "empty_model_result",
      "reason": reason,
      "turn": turn,
      "instruction": instruction,
      "model_result_keys": sorted(str(key) for key in result.keys()),
    },
  }


def _is_empty_model_diagnostic(output: dict[str, Any]) -> bool:
  diagnostics = output.get("diagnostics")
  return isinstance(diagnostics, dict) and diagnostics.get("type") == "empty_model_result"


def _is_blank_no_tool_diagnostic(output: dict[str, Any] | None) -> bool:
  if output is None:
    return False
  diagnostics = output.get("diagnostics")
  return (
    isinstance(diagnostics, dict)
    and diagnostics.get("type") == "empty_model_result"
    and "reason" not in diagnostics
  )


def _model_result_text(result: dict[str, Any]) -> str:
  output = result.get("output", result.get("content"))
  if isinstance(output, str):
    return output
  if isinstance(output, dict):
    parts = []
    for key in ("content", "summary", "value", "text"):
      value = output.get(key)
      if isinstance(value, str):
        parts.append(value)
    return "\n".join(parts)
  return ""


def _looks_like_unexecuted_large_code(content: str) -> bool:
  code_block_pattern = r"```[a-zA-Z0-9_+-]*\n[\s\S]{50,}?```"
  blocks = re.findall(code_block_pattern, content)
  if len(blocks) != 1:
    return False
  match = re.search(code_block_pattern, content)
  if match is None or content[match.end() :].strip():
    return False
  residual = content.replace(match.group(0), "")
  residual = re.sub(r"<thinking>[\s\S]*?</thinking>", "", residual, flags=re.IGNORECASE)
  residual = re.sub(r"<summary>[\s\S]*?</summary>", "", residual, flags=re.IGNORECASE)
  return len(re.sub(r"\s+", "", residual)) <= 30


def _progress_guard_instruction(turn: int) -> str:
  if turn % 75 == 0:
    return (
      " [DANGER] This task has run for many turns. Stop ineffective retries; summarize current evidence, "
      "state the blocker, and request user input if progress requires external help."
    )
  if turn % 7 == 0:
    return (
      " [DANGER] Avoid ineffective retries. If there is no new progress, switch strategy: inspect the actual "
      "environment/state, use a different capability/source, or request user input with the concrete blocker."
    )
  return ""


def _step_outcome_from_record(record: ContinuousToolCallRecord) -> ContinuousStepOutcome:
  return ContinuousStepOutcome(
    tool_name=record.name,
    capability_id=record.capability_id,
    ok=record.ok,
    model_visible_result=_compact_for_json(record.output, max_bytes=6000),
    error=_compact_for_json(record.error, max_bytes=2000),
    anchor_update=_anchor_update_from_record(record),
  )


def _ensure_tool_call_ids(tool_calls: list[dict[str, Any]], *, turn: int) -> list[dict[str, Any]]:
  normalized: list[dict[str, Any]] = []
  for index, raw_call in enumerate(tool_calls):
    call = dict(raw_call)
    if not isinstance(call.get("id"), str) or not call.get("id"):
      call["id"] = f"call_turn_{turn}_{index}"
    normalized.append(call)
  return normalized


def _tool_call_id(raw_call: dict[str, Any]) -> str:
  call_id = raw_call.get("id") or raw_call.get("tool_call_id")
  return str(call_id) if call_id else ""


def _assistant_transcript_message(model_result: dict[str, Any], tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
  content = _model_result_text(model_result)
  message: dict[str, Any] = {
    "role": "assistant",
    "content": content,
    "tool_calls": [
      _openai_tool_call(call)
      for call in tool_calls
    ],
  }
  return message


def _openai_tool_call(call: dict[str, Any]) -> dict[str, Any]:
  call_id = _tool_call_id(call)
  name = str(call.get("name") or "")
  arguments = call.get("input", call.get("arguments", {}))
  if not isinstance(arguments, str):
    arguments = to_json(arguments if isinstance(arguments, dict) else {})
  return {
    "id": call_id,
    "type": "function",
    "function": {
      "name": name,
      "arguments": arguments,
    },
  }


def _tool_transcript_message(raw_call: dict[str, Any], result: dict[str, Any], *, name: str) -> dict[str, Any]:
  return {
    "role": "tool",
    "tool_call_id": _tool_call_id(raw_call),
    "name": name,
    "content": to_json(result),
  }


def _anchor_update_from_record(record: ContinuousToolCallRecord) -> str:
  status = "ok" if record.ok else f"failed:{(record.error or {}).get('type', 'unknown_error')}"
  return f"{record.name} {status}"


def _summarize_turn_records(records: list[ContinuousToolCallRecord], *, turn: int) -> list[str]:
  summaries: list[str] = []
  for record in records:
    status = "ok" if record.ok else f"failed:{(record.error or {}).get('type', 'unknown')}"
    details: list[str] = []
    target_id = _record_target_id(record)
    if target_id:
      details.append(f"target={target_id}")
    url = _record_url(record)
    if url:
      details.append(f"url={url}")
    page = record.output.get("page")
    if isinstance(page, dict):
      title = page.get("title")
      if isinstance(title, str) and title.strip():
        details.append(f"page={title.strip()[:80]}")
      feed_titles = page.get("feed_titles")
      if isinstance(feed_titles, list) and feed_titles:
        details.append(f"feed_titles={len(feed_titles)}")
    suffix = " " + " ".join(details) if details else ""
    summaries.append(f"turn {turn}: {record.name} {status}{suffix}")
  return summaries


def _summarize_hook_results(results: list[ProgressHookResult], *, turn: int) -> list[str]:
  summaries: list[str] = []
  for result in results:
    if result.severity not in {"warning", "blocking", "terminal"}:
      continue
    summaries.append(f"turn {turn}: execution_hook {result.severity}:{result.reason}")
  return summaries


def _record_target_id(record: ContinuousToolCallRecord) -> str | None:
  for source in (record.output, record.input):
    value = source.get("target_id") or source.get("active_target_id")
    if isinstance(value, str) and value:
      return value
  scope = record.output.get("browser_scope")
  if isinstance(scope, dict):
    value = scope.get("active_target_id")
    if isinstance(value, str) and value:
      return value
  return None


def _record_url(record: ContinuousToolCallRecord) -> str | None:
  value = record.output.get("url")
  if isinstance(value, str) and value:
    return value
  payload = record.input.get("payload")
  if isinstance(payload, dict):
    value = payload.get("url")
    if isinstance(value, str) and value:
      return value
  return None


def _has_displayable_output(output: dict[str, Any]) -> bool:
  for key in ("content", "summary", "value"):
    value = output.get(key)
    if isinstance(value, str) and value.strip():
      return True
  rich_keys = {
    "workbench",
    "delegation",
    "delegations",
    "page",
    "results",
    "search_results",
    "feed_titles",
    "visible_cards",
    "diagnostics",
  }
  return any(key in output for key in rich_keys)


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
    return _extract_browser_observation_from_tool_result(output)
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


def _extract_browser_observation_from_tool_result(output: dict[str, Any]) -> dict[str, Any] | None:
  if "targets" in output and "page" not in output:
    return None
  payload = _browser_tool_payload(output)
  if payload is None:
    return None
  observation = _observation_from_structured_browser_payload(payload)
  if observation:
    for key in ("url", "title"):
      value = output.get(key)
      if isinstance(value, str) and value.strip() and key not in observation:
        observation[key] = value.strip()
    return observation
  if isinstance(payload, str) and payload.strip():
    return {"text": payload.strip()}
  return None


def _browser_tool_payload(output: dict[str, Any]) -> Any:
  for key in ("js_return", "data"):
    value = output.get(key)
    if value is not None:
      return _parse_browser_payload(value)
  result = output.get("result")
  if isinstance(result, dict) and "js_return" in result:
    return _parse_browser_payload(result.get("js_return"))
  if isinstance(result, dict) and "data" in result:
    data = result.get("data")
    if isinstance(data, dict) and "js_return" in data:
      return _parse_browser_payload(data.get("js_return"))
    return _parse_browser_payload(data)
  if result is not None:
    return _parse_browser_payload(result)
  return None


def _parse_browser_payload(value: Any) -> Any:
  if isinstance(value, str):
    text = value.strip()
    if not text:
      return value
    try:
      return json.loads(text)
    except json.JSONDecodeError:
      return value
  return value


def _observation_from_structured_browser_payload(payload: Any) -> dict[str, Any]:
  if isinstance(payload, list):
    if _looks_like_browser_target_list(payload):
      return {}
    cards = [_normalize_browser_card(item) for item in payload]
    cards = [item for item in cards if item]
    if not cards:
      return {}
    titles = [str(item["title"]) for item in cards if item.get("title")]
    lines = [_browser_card_line(item) for item in cards]
    return {
      "feed_titles": _unique_strings(titles),
      "visible_cards": _unique_strings([line for line in lines if line]),
    }
  if not isinstance(payload, dict):
    return {}
  observation: dict[str, Any] = {}
  for key in ("title", "url", "text", "summary"):
    value = payload.get(key)
    if isinstance(value, str) and value.strip():
      target_key = "text" if key == "summary" else key
      observation[target_key] = value.strip()
  for key, target_key in (
    ("feed_titles", "feed_titles"),
    ("titles", "feed_titles"),
    ("visible_cards", "visible_cards"),
    ("cards", "visible_cards"),
  ):
    value = payload.get(key)
    if isinstance(value, list):
      if all(isinstance(item, dict) for item in value):
        nested = _observation_from_structured_browser_payload(value)
        for nested_key, nested_value in nested.items():
          if nested_value:
            observation.setdefault(nested_key, nested_value)
      else:
        items = [str(item).strip() for item in value if str(item).strip()]
        if items:
          observation[target_key] = _unique_strings(items)
  for key in ("items", "posts", "notes", "results", "data"):
    value = payload.get(key)
    if isinstance(value, list):
      nested = _observation_from_structured_browser_payload(value)
      for nested_key, nested_value in nested.items():
        if nested_value and nested_key not in observation:
          observation[nested_key] = nested_value
  return observation


def _looks_like_browser_target_list(payload: list[Any]) -> bool:
  dict_items = [item for item in payload if isinstance(item, dict)]
  if not dict_items or len(dict_items) != len(payload):
    return False
  target_like = 0
  for item in dict_items:
    keys = set(item)
    has_target_id = any(key in keys for key in ("target_id", "id", "sessionId"))
    has_tab_metadata = any(key in keys for key in ("windowId", "active", "kind", "metadata"))
    has_location = any(key in keys for key in ("url", "title", "label"))
    has_content_field = any(
      key in keys
      for key in (
        "text",
        "content",
        "summary",
        "displayTitle",
        "feed_titles",
        "visible_cards",
        "search_results",
        "snippet",
      )
    )
    if has_target_id and has_location and (has_tab_metadata or not has_content_field):
      target_like += 1
  return target_like == len(dict_items)


def _normalize_browser_card(value: Any) -> dict[str, str]:
  if isinstance(value, str):
    text = value.strip()
    return {"text": text, "title": text} if text else {}
  if not isinstance(value, dict):
    return {}
  card: dict[str, str] = {}
  for source_key, target_key in (
    ("title", "title"),
    ("displayTitle", "title"),
    ("name", "title"),
    ("text", "text"),
    ("content", "text"),
    ("author", "author"),
    ("user", "author"),
    ("time", "time"),
    ("url", "url"),
    ("href", "url"),
    ("metrics", "metrics"),
  ):
    raw = value.get(source_key)
    if isinstance(raw, str) and raw.strip():
      card[target_key] = raw.strip()
    elif raw is not None and target_key == "metrics":
      card[target_key] = str(raw)
  return card


def _browser_card_line(card: dict[str, str]) -> str:
  primary = card.get("title") or card.get("text") or ""
  if not primary:
    return ""
  details = []
  for key in ("author", "time", "metrics", "url"):
    value = card.get(key)
    if value:
      details.append(value)
  return primary if not details else f"{primary}（{'，'.join(details)}）"


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
