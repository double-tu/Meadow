"""Workspace isolation and patch review orchestration."""

from __future__ import annotations

from dataclasses import replace
from typing import Protocol

from agent_kernel.domain.base import new_id, utc_now
from agent_kernel.domain.identifiers import ArtifactRef
from agent_kernel.domain.interaction import PatchArtifact, ReviewRecord, WorkspaceLease


class WorkspaceBackend(Protocol):
  def allocate(self, task_id: str, agent_session_id: str, base_ref: str | None = None) -> str:
    ...

  def release(self, workspace_uri: str) -> None:
    ...


class PatchReviewBackend(Protocol):
  def merge(self, patch: PatchArtifact) -> ArtifactRef:
    ...


class FakeWorkspaceBackend:
  def __init__(self, root_uri: str = "workspace://isolated") -> None:
    self.root_uri = root_uri.rstrip("/")
    self.allocated: list[tuple[str, str, str]] = []
    self.released: list[str] = []

  def allocate(self, task_id: str, agent_session_id: str, base_ref: str | None = None) -> str:
    workspace_uri = f"{self.root_uri}/{task_id}/{agent_session_id}"
    self.allocated.append((task_id, agent_session_id, workspace_uri))
    return workspace_uri

  def release(self, workspace_uri: str) -> None:
    self.released.append(workspace_uri)


class ArtifactMergeBackend:
  def merge(self, patch: PatchArtifact) -> ArtifactRef:
    return ArtifactRef(
      artifact_id=new_id("merged_patch"),
      uri=f"artifact://merged/{patch.patch_id}",
      media_type="application/vnd.agent-kernel.patch",
    )


class WorkspaceIsolationService:
  def __init__(
    self,
    uow_factory,
    workspace_backend: WorkspaceBackend,
    review_backend: PatchReviewBackend | None = None,
  ) -> None:
    self._uow_factory = uow_factory
    self._workspace_backend = workspace_backend
    self._review_backend = review_backend or ArtifactMergeBackend()

  def allocate(
    self,
    task_id: str,
    agent_session_id: str,
    base_ref: str | None = None,
    isolation_mode: str = "worktree",
  ) -> WorkspaceLease:
    workspace_uri = self._workspace_backend.allocate(task_id, agent_session_id, base_ref)
    lease = WorkspaceLease(
      lease_id=new_id("workspace_lease"),
      task_id=task_id,
      agent_session_id=agent_session_id,
      workspace_uri=workspace_uri,
      base_ref=base_ref,
      isolation_mode=isolation_mode,  # type: ignore[arg-type]
    )
    with self._uow_factory() as uow:
      uow.interactions.save_workspace_lease(lease)
    return lease

  def release(self, lease_id: str) -> WorkspaceLease:
    lease = self._get_lease(lease_id)
    self._workspace_backend.release(lease.workspace_uri)
    released = replace(lease, status="released", updated_at=utc_now())
    with self._uow_factory() as uow:
      uow.interactions.save_workspace_lease(released)
    return released

  def submit_patch(
    self,
    lease_id: str,
    artifact_ref: ArtifactRef,
    summary: str,
  ) -> PatchArtifact:
    lease = self._get_lease(lease_id)
    if lease.status != "active":
      raise ValueError(f"Workspace lease is not active: {lease.status}")
    patch = PatchArtifact(
      patch_id=new_id("patch"),
      lease_id=lease.lease_id,
      task_id=lease.task_id,
      author_session_id=lease.agent_session_id,
      artifact_ref=artifact_ref,
      summary=summary,
      status="submitted",
    )
    with self._uow_factory() as uow:
      uow.interactions.save_patch_artifact(patch)
    return patch

  def review(
    self,
    patch_id: str,
    reviewer_id: str,
    decision: str,
    comments: list[str] | None = None,
  ) -> ReviewRecord:
    patch = self._get_patch(patch_id)
    review = ReviewRecord(
      review_id=new_id("review"),
      patch_id=patch.patch_id,
      reviewer_id=reviewer_id,
      decision=decision,  # type: ignore[arg-type]
      comments=comments or [],
    )
    next_status = "approved" if decision == "approved" else "rejected"
    with self._uow_factory() as uow:
      uow.interactions.save_review_record(review)
      uow.interactions.save_patch_artifact(
        replace(patch, status=next_status, updated_at=utc_now())  # type: ignore[arg-type]
      )
    return review

  def merge(self, patch_id: str) -> PatchArtifact:
    patch = self._get_patch(patch_id)
    if patch.status != "approved":
      raise ValueError(f"Patch must be approved before merge: {patch.status}")
    merged_ref = self._review_backend.merge(patch)
    merged = replace(patch, status="merged", artifact_ref=merged_ref, updated_at=utc_now())
    with self._uow_factory() as uow:
      uow.artifacts.save(merged_ref, metadata={"source_patch_id": patch.patch_id})
      uow.interactions.save_patch_artifact(merged)
    return merged

  def _get_lease(self, lease_id: str) -> WorkspaceLease:
    with self._uow_factory() as uow:
      lease = uow.interactions.get_workspace_lease(lease_id)
    if lease is None:
      raise KeyError(f"Workspace lease not found: {lease_id}")
    return lease

  def _get_patch(self, patch_id: str) -> PatchArtifact:
    with self._uow_factory() as uow:
      patch = uow.interactions.get_patch_artifact(patch_id)
    if patch is None:
      raise KeyError(f"Patch artifact not found: {patch_id}")
    return patch
