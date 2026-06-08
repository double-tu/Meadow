"""Context budget allocation."""

from dataclasses import dataclass

from agent_kernel.domain.context import ContextLayer, ContextLayerItem, LayerBudget


@dataclass(slots=True)
class ContextBudget:
  max_tokens: int
  message_tokens: int
  memory_tokens: int

  @classmethod
  def split(cls, max_tokens: int, memory_ratio: float = 0.4) -> "ContextBudget":
    memory_tokens = int(max_tokens * memory_ratio)
    return cls(
      max_tokens=max_tokens,
      message_tokens=max_tokens - memory_tokens,
      memory_tokens=memory_tokens,
    )


class ContextBudgetManager:
  """Allocates and trims token budget across context layers."""

  DEFAULT_LAYER_WEIGHTS: dict[str, float] = {
    "system_policy": 0.12,
    "agent_profile": 0.06,
    "skill_tool_index": 0.16,
    "working_memory": 0.16,
    "conversation_window": 0.26,
    "episodic_artifact": 0.12,
    "long_term_memory": 0.10,
  }

  def allocate(self, max_tokens: int, layer_weights: dict[str, float] | None = None) -> LayerBudget:
    weights = {**self.DEFAULT_LAYER_WEIGHTS, **(layer_weights or {})}
    total = sum(weight for weight in weights.values() if weight > 0)
    if total <= 0:
      raise ValueError("At least one context layer must have a positive budget weight.")
    layer_budgets = {
      layer: int(max_tokens * (weight / total))
      for layer, weight in weights.items()
      if weight > 0
    }
    allocated = sum(layer_budgets.values())
    if layer_budgets and allocated < max_tokens:
      first_key = next(iter(layer_budgets))
      layer_budgets[first_key] += max_tokens - allocated
    return LayerBudget(max_tokens=max_tokens, layer_budgets=layer_budgets)

  def trim_layer(self, layer: ContextLayer, budget_tokens: int | None = None) -> ContextLayer:
    budget = layer.budget_tokens if budget_tokens is None else budget_tokens
    selected: list[ContextLayerItem] = []
    omitted: list[ContextLayerItem] = []
    used = 0
    sorted_items = sorted(layer.items, key=lambda candidate: candidate.priority, reverse=True)
    for item in sorted_items:
      if item.token_estimate <= 0 or used + item.token_estimate <= budget:
        selected.append(item)
        used += max(0, item.token_estimate)
      else:
        omitted.append(item)
    if not selected and sorted_items and budget > 0:
      selected.append(sorted_items[0])
      omitted = [item for item in sorted_items[1:]]
      used = max(0, sorted_items[0].token_estimate)
    selected_ids = {item.item_id for item in selected}
    omitted_ids = {item.item_id for item in omitted}
    return ContextLayer(
      kind=layer.kind,
      budget_tokens=budget,
      items=[item for item in layer.items if item.item_id in selected_ids],
      omitted_items=[*layer.omitted_items, *[item for item in layer.items if item.item_id in omitted_ids]],
      token_estimate=used,
      rationale=layer.rationale,
    )
