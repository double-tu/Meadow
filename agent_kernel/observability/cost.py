"""Cost ledger aggregation."""

from dataclasses import dataclass

from agent_kernel.domain.base import DomainModel


@dataclass(slots=True)
class CostLedger(DomainModel):
  run_id: str
  model_calls: int = 0
  tool_calls: int = 0
  input_tokens: int = 0
  output_tokens: int = 0
  cost_usd: float = 0.0


class CostService:
  def __init__(self, uow_factory) -> None:
    self._uow_factory = uow_factory

  def build_ledger(self, run_id: str) -> CostLedger:
    ledger = CostLedger(run_id=run_id)
    with self._uow_factory() as uow:
      events = uow.events.list_by_run(run_id)
      tool_calls = uow.tool_calls.list_by_run(run_id)
    ledger.tool_calls = len(tool_calls)
    for event in events:
      usage = event.payload.get("usage")
      if not isinstance(usage, dict):
        continue
      if str(event.event_type.value).startswith("model.call."):
        ledger.model_calls += 1
      ledger.input_tokens += int(usage.get("input_tokens", 0))
      ledger.output_tokens += int(usage.get("output_tokens", 0))
      ledger.cost_usd += float(usage.get("cost_usd", 0.0))
    return ledger

