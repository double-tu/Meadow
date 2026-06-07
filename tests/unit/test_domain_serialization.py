import unittest

from agent_kernel.domain import (
  ArtifactRef,
  MemoryRef,
  ModelContext,
  RuntimeEvent,
  RuntimeEventType,
)
from agent_kernel.domain.errors import DomainValidationError


class DomainSerializationTests(unittest.TestCase):
  def test_runtime_event_round_trips_nested_refs(self) -> None:
    event = RuntimeEvent(
      event_type=RuntimeEventType.TOOL_CALL_COMPLETED,
      run_id="run_1",
      payload={"ok": True},
      artifact_refs=[ArtifactRef(artifact_id="art_1", uri="artifact://art_1")],
    )

    restored = RuntimeEvent.from_dict(event.to_dict())

    self.assertEqual(restored.event_type, RuntimeEventType.TOOL_CALL_COMPLETED)
    self.assertEqual(restored.run_id, "run_1")
    self.assertEqual(restored.artifact_refs[0].artifact_id, "art_1")

  def test_event_rejects_large_payload(self) -> None:
    with self.assertRaises(DomainValidationError):
      RuntimeEvent(
        event_type=RuntimeEventType.ARTIFACT_CREATED,
        run_id="run_1",
        payload={"raw": "x" * 9000},
      )

  def test_model_context_serializes_memory_refs(self) -> None:
    context = ModelContext(
      messages=[{"role": "user", "content": "build"}],
      memory_refs=[MemoryRef(memory_id="mem_1", memory_type="working", score=0.9)],
      inclusion_rationale={"mem_1": "latest task constraint"},
    )

    data = context.to_dict()
    restored = ModelContext.from_dict(data)

    self.assertEqual(data["memory_refs"][0]["memory_id"], "mem_1")
    self.assertEqual(restored.memory_refs[0].score, 0.9)


if __name__ == "__main__":
  unittest.main()

