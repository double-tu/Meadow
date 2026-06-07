"""Context manager that builds model context from messages and memory."""

from __future__ import annotations

from agent_kernel.context.budget import ContextBudget
from agent_kernel.context.token_counter import estimate_tokens
from agent_kernel.domain.base import new_id
from agent_kernel.domain.context import ContextCandidate, ContextPlan, ModelContext, RetrievalPack
from agent_kernel.domain.events import RuntimeEvent, RuntimeEventType
from agent_kernel.domain.identifiers import ArtifactRef, MemoryRef
from agent_kernel.domain.memory import MemoryItem
from agent_kernel.memory.facade import MemoryFacade


class ContextManager:
  def __init__(self, uow_factory, memory: MemoryFacade, max_tokens: int = 2048) -> None:
    self._uow_factory = uow_factory
    self._memory = memory
    self._max_tokens = max_tokens

  def build(
    self,
    run_id: str,
    scope: str,
    model_ref: str,
    messages: list[dict[str, object]],
    allowed_sensitivities: set[str] | None = None,
    available_tools: list[dict[str, object]] | None = None,
    tool_allowlist: set[str] | None = None,
  ) -> ModelContext:
    budget = ContextBudget.split(self._max_tokens)
    memories = self._filter_memories(
      self._memory.retrieve(scope, limit=20),
      allowed_sensitivities or {"public", "internal"},
    )
    candidates = self._build_memory_candidates(memories)
    selected, omitted = self._select_memory_candidates(candidates, budget.memory_tokens)
    selected_refs = [
      MemoryRef(memory_id=candidate.source_ref, memory_type="working", score=candidate.relevance_score)
      for candidate in selected
    ]
    for ref in selected_refs:
      with self._uow_factory() as uow:
        uow.memory.mark_used(ref.memory_id)
    tool_schemas = self._prune_tools(available_tools or [], tool_allowlist)
    quality_warnings = []
    if omitted:
      quality_warnings.append("memory_budget_omitted_candidates")
    if available_tools and len(tool_schemas) < len(available_tools):
      quality_warnings.append("tool_visibility_pruned")
    plan = ContextPlan(
      plan_id=new_id("ctx_plan"),
      run_id=run_id,
      model_ref=model_ref,
      partition_budgets={"messages": budget.message_tokens, "memory": budget.memory_tokens},
      candidates=[*selected, *omitted],
      selected_candidate_ids=[candidate.candidate_id for candidate in selected],
      omitted_candidate_ids=[candidate.candidate_id for candidate in omitted],
      tool_visibility=[str(tool.get("name", tool.get("capability_id", ""))) for tool in tool_schemas],
      density_score=self._density_score(selected, budget.memory_tokens),
      compression_strategy="omit_over_budget",
      quality_warnings=quality_warnings,
    )
    context = ModelContext(
      messages=[
        *messages,
        *[
          {
            "role": "system",
            "content": {"memory_id": memory.memory_id, "content": memory.content},
          }
          for memory in memories
          if memory.memory_id in {ref.memory_id for ref in selected_refs}
        ],
      ],
      tool_schemas=tool_schemas,
      attachments=self._artifact_refs_for_selected(memories, selected_refs),
      memory_refs=selected_refs,
      omitted_candidates=[
        MemoryRef(memory_id=candidate.source_ref, memory_type="working", score=candidate.relevance_score)
        for candidate in omitted
      ],
      token_budget_ledger=plan.partition_budgets,
      inclusion_rationale={
        candidate.source_ref: candidate.rationale or "Selected by importance within budget."
        for candidate in selected
      },
    )
    self._write_ledger(
      run_id,
      plan,
      RetrievalPack(
        working_snapshot={},
        artifact_refs=self._artifact_refs_for_selected(memories, selected_refs),
        token_estimate=self._max_tokens,
      ),
    )
    return context

  @staticmethod
  def _filter_memories(
    memories: list[MemoryItem],
    allowed_sensitivities: set[str],
  ) -> list[MemoryItem]:
    return [memory for memory in memories if memory.sensitivity in allowed_sensitivities]

  @staticmethod
  def _build_memory_candidates(memories: list[MemoryItem]) -> list[ContextCandidate]:
    candidates: list[ContextCandidate] = []
    for memory in memories:
      importance = memory.importance if memory.importance is not None else 0.5
      candidates.append(
        ContextCandidate(
          candidate_id=new_id("ctx_candidate"),
          source_type="memory",
          source_ref=memory.memory_id,
          token_estimate=estimate_tokens(memory.content),
          relevance_score=importance,
          sensitivity=memory.sensitivity,
          rationale="Selected by memory importance.",
        )
      )
    return candidates

  @staticmethod
  def _select_memory_candidates(
    candidates: list[ContextCandidate],
    budget_tokens: int,
  ) -> tuple[list[ContextCandidate], list[ContextCandidate]]:
    selected: list[ContextCandidate] = []
    omitted: list[ContextCandidate] = []
    used = 0
    for candidate in sorted(candidates, key=lambda item: item.relevance_score, reverse=True):
      if used + candidate.token_estimate <= budget_tokens:
        selected.append(candidate)
        used += candidate.token_estimate
      else:
        omitted.append(candidate)
    return selected, omitted

  @staticmethod
  def _prune_tools(
    tools: list[dict[str, object]],
    allowlist: set[str] | None,
  ) -> list[dict[str, object]]:
    if allowlist is None:
      return tools
    return [
      tool
      for tool in tools
      if str(tool.get("name", tool.get("capability_id", ""))) in allowlist
    ]

  @staticmethod
  def _artifact_refs_for_selected(
    memories: list[MemoryItem],
    selected_refs: list[MemoryRef],
  ) -> list[ArtifactRef]:
    selected_ids = {ref.memory_id for ref in selected_refs}
    refs: list[ArtifactRef] = []
    for memory in memories:
      if memory.memory_id in selected_ids:
        refs.extend(memory.source_artifact_refs)
    return refs

  @staticmethod
  def _density_score(selected: list[ContextCandidate], budget_tokens: int) -> float:
    if budget_tokens <= 0:
      return 0.0
    useful_tokens = sum(
      int(candidate.token_estimate * candidate.relevance_score)
      for candidate in selected
    )
    return round(min(1.0, useful_tokens / budget_tokens), 4)

  def _write_ledger(self, run_id: str, plan: ContextPlan, retrieval_pack: RetrievalPack) -> None:
    with self._uow_factory() as uow:
      uow.events.append(
        RuntimeEvent(
          event_type=RuntimeEventType.CONTEXT_BUILT,
          run_id=run_id,
          payload={"context_plan": plan.to_dict(), "retrieval_pack": retrieval_pack.to_dict()},
        )
      )
