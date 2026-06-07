"""Workspace isolation and patch review orchestration."""

from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import re
import subprocess
from typing import Protocol, cast
from urllib.parse import unquote, urlparse

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


class GitWorktreeBackend:
  """Git-backed workspace backend for isolated agent worktrees."""

  def __init__(
    self,
    repo_path: str | Path,
    worktree_root: str | Path,
    *,
    target_ref: str = "HEAD",
    git_binary: str = "git",
  ) -> None:
    self.repo_path = Path(repo_path).resolve()
    self.worktree_root = Path(worktree_root).resolve()
    self.target_ref = target_ref
    self.git_binary = git_binary

  def allocate(self, task_id: str, agent_session_id: str, base_ref: str | None = None) -> str:
    self.worktree_root.mkdir(parents=True, exist_ok=True)
    branch = self._branch_name(task_id, agent_session_id)
    path = self.worktree_root / branch
    if path.exists():
      raise FileExistsError(f"Workspace path already exists: {path}")
    self._git(["worktree", "add", "-b", branch, str(path), base_ref or self.target_ref])
    return path.as_uri()

  def release(self, workspace_uri: str) -> None:
    path = self._path_from_uri(workspace_uri)
    self._git(["worktree", "remove", "--force", str(path)])

  def merge(self, patch: PatchArtifact) -> ArtifactRef:
    workspace_path = self._workspace_path_from_patch(patch)
    branch = self._current_branch(workspace_path)
    self._git(["fetch", ".", branch], cwd=self.repo_path)
    try:
      self._git(["merge", "--no-ff", "--no-edit", f"FETCH_HEAD"], cwd=self.repo_path)
    except RuntimeError as exc:
      self._git(["merge", "--abort"], cwd=self.repo_path, check=False)
      raise RuntimeError(f"Git worktree merge failed for {patch.patch_id}: {exc}") from exc
    commit = self._git(["rev-parse", "HEAD"], cwd=self.repo_path).strip()
    return ArtifactRef(
      artifact_id=new_id("merged_patch"),
      uri=f"git://merge/{commit}",
      media_type="application/vnd.agent-kernel.git-merge",
      version=commit,
    )

  def create_patch_ref(self, workspace_uri: str, *, artifact_id: str | None = None) -> ArtifactRef:
    workspace_path = self._path_from_uri(workspace_uri)
    diff = self._git(["diff", "--binary", "HEAD"], cwd=workspace_path)
    staged_diff = self._git(["diff", "--binary", "--cached", "HEAD"], cwd=workspace_path)
    combined_diff = diff + staged_diff
    return ArtifactRef(
      artifact_id=artifact_id or new_id("git_patch"),
      uri=f"{workspace_uri}#diff",
      media_type="text/x-diff",
      checksum=hashlib.sha256(combined_diff.encode("utf-8")).hexdigest(),
    )

  def commit_all(self, workspace_uri: str, message: str) -> str:
    workspace_path = self._path_from_uri(workspace_uri)
    self._git(["add", "-A"], cwd=workspace_path)
    status = self._git(["status", "--porcelain"], cwd=workspace_path)
    if not status.strip():
      return self._git(["rev-parse", "HEAD"], cwd=workspace_path).strip()
    self._git(["commit", "-m", message], cwd=workspace_path)
    return self._git(["rev-parse", "HEAD"], cwd=workspace_path).strip()

  def _git(
    self,
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
  ) -> str:
    result = subprocess.run(
      [self.git_binary, *args],
      cwd=str(cwd or self.repo_path),
      text=True,
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      check=False,
    )
    if check and result.returncode != 0:
      raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "git command failed")
    return result.stdout

  def _current_branch(self, workspace_path: Path) -> str:
    return self._git(["branch", "--show-current"], cwd=workspace_path).strip()

  @classmethod
  def _workspace_path_from_patch(cls, patch: PatchArtifact) -> Path:
    workspace_uri = patch.artifact_ref.uri.split("#", 1)[0]
    return cls._path_from_uri(workspace_uri)

  @staticmethod
  def _path_from_uri(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
      raise ValueError(f"Git worktree backend requires file:// workspace uri: {uri}")
    return Path(unquote(parsed.path)).resolve()

  @staticmethod
  def _branch_name(task_id: str, agent_session_id: str) -> str:
    raw = f"meadow/{task_id}/{agent_session_id}/{new_id('wt')[:11]}"
    return re.sub(r"[^A-Za-z0-9._/-]+", "-", raw).strip("-/.") or f"meadow/{new_id('workspace')}"


class GitPatchReviewBackend:
  """Patch review backend that merges an approved git worktree branch."""

  def __init__(self, git_backend: GitWorktreeBackend) -> None:
    self._git_backend = git_backend

  def merge(self, patch: PatchArtifact) -> ArtifactRef:
    return self._git_backend.merge(patch)


class WorkspaceIsolationService:
  def __init__(
    self,
    uow_factory,
    workspace_backend: WorkspaceBackend,
    review_backend: PatchReviewBackend | None = None,
  ) -> None:
    self._uow_factory = uow_factory
    self._workspace_backend = workspace_backend
    self._review_backend = review_backend or (
      cast(PatchReviewBackend, workspace_backend) if hasattr(workspace_backend, "merge") else ArtifactMergeBackend()
    )

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
