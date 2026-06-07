"""Artifact inspection aggregation."""

from dataclasses import dataclass, field

from agent_kernel.domain.base import DomainModel
from agent_kernel.domain.identifiers import ArtifactRef


@dataclass(slots=True)
class ArtifactInspection(DomainModel):
  run_id: str
  artifact_refs: list[ArtifactRef] = field(default_factory=list)
  missing_artifact_ids: list[str] = field(default_factory=list)


class ArtifactInspectionService:
  def __init__(self, uow_factory) -> None:
    self._uow_factory = uow_factory

  def inspect_run(self, run_id: str) -> ArtifactInspection:
    artifact_ids: list[str] = []
    with self._uow_factory() as uow:
      state = uow.states.get(run_id)
      if state is not None:
        artifact_ids.extend(ref.artifact_id for ref in state.artifact_refs)
      for event in uow.events.list_by_run(run_id):
        artifact_ids.extend(ref.artifact_id for ref in event.artifact_refs)
      unique_ids = list(dict.fromkeys(artifact_ids))
      refs = uow.artifacts.list_by_ids(unique_ids)
    found_ids = {ref.artifact_id for ref in refs}
    return ArtifactInspection(
      run_id=run_id,
      artifact_refs=refs,
      missing_artifact_ids=[artifact_id for artifact_id in unique_ids if artifact_id not in found_ids],
    )

