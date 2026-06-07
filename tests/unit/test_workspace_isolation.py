from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from urllib.parse import unquote, urlparse

from agent_kernel.agents import FakeWorkspaceBackend, GitWorktreeBackend, WorkspaceIsolationService, WorkspaceMergeQueueService
from agent_kernel.domain import ArtifactRef, PatchArtifact
from agent_kernel.persistence import UnitOfWork, connect_sqlite
from agent_kernel.runtime import unit_of_work_factory


class WorkspaceIsolationTests(unittest.TestCase):
  def test_workspace_patch_review_and_merge_flow(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      backend = FakeWorkspaceBackend()
      service = WorkspaceIsolationService(uow_factory, backend)

      lease = service.allocate(
        task_id="task_1",
        agent_session_id="session_1",
        base_ref="main",
      )
      patch = service.submit_patch(
        lease.lease_id,
        ArtifactRef(
          artifact_id="patch_artifact_1",
          uri="artifact://patches/patch_1",
          media_type="text/x-diff",
        ),
        summary="Implement API route.",
      )
      review = service.review(
        patch.patch_id,
        reviewer_id="reviewer_1",
        decision="approved",
        comments=["Looks safe."],
      )
      merged = service.merge(patch.patch_id)
      released = service.release(lease.lease_id)

      with UnitOfWork(conn) as uow:
        leases = uow.interactions.list_workspace_leases("task_1")
        patches = uow.interactions.list_patch_artifacts(lease.lease_id)
        reviews = uow.interactions.list_review_records(patch.patch_id)
        merged_artifact = uow.artifacts.get(merged.artifact_ref.artifact_id)

      self.assertEqual(lease.workspace_uri, "workspace://isolated/task_1/session_1")
      self.assertEqual(backend.allocated[0][0], "task_1")
      self.assertEqual(review.decision, "approved")
      self.assertEqual(merged.status, "merged")
      self.assertEqual(released.status, "released")
      self.assertEqual(backend.released, [lease.workspace_uri])
      self.assertEqual(leases[-1].status, "released")
      self.assertEqual(patches[-1].status, "merged")
      self.assertEqual(reviews[0].comments, ["Looks safe."])
      self.assertIsNotNone(merged_artifact)
    finally:
      conn.close()

  def test_workspace_merge_queue_processes_approved_patches_in_priority_order(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      merge_backend = _RecordingMergeBackend()
      service = WorkspaceIsolationService(uow_factory, FakeWorkspaceBackend(), review_backend=merge_backend)
      queue = WorkspaceMergeQueueService(uow_factory, service)
      first_lease = service.allocate("task_queue", "session_1")
      second_lease = service.allocate("task_queue", "session_2")
      first_patch = service.submit_patch(
        first_lease.lease_id,
        ArtifactRef("patch_a", "artifact://patch/a"),
        "first",
      )
      second_patch = service.submit_patch(
        second_lease.lease_id,
        ArtifactRef("patch_b", "artifact://patch/b"),
        "second",
      )
      service.review(first_patch.patch_id, "reviewer", "approved")
      service.review(second_patch.patch_id, "reviewer", "approved")
      low_priority = queue.enqueue(first_patch.patch_id, priority=50)
      high_priority = queue.enqueue(second_patch.patch_id, priority=10)

      processed = queue.process_all("task_queue")
      queue_items = queue.list("task_queue")

      self.assertEqual([item.queue_item_id for item in processed], [high_priority.queue_item_id, low_priority.queue_item_id])
      self.assertEqual([patch.patch_id for patch in merge_backend.merged], [second_patch.patch_id, first_patch.patch_id])
      self.assertEqual([item.status for item in queue_items], ["merged", "merged"])
      self.assertTrue(all(item.merged_artifact_ref is not None for item in queue_items))
    finally:
      conn.close()

  def test_workspace_merge_queue_records_failure_and_stops(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      merge_backend = _RecordingMergeBackend(fail_patch_id="patch_fail")
      service = WorkspaceIsolationService(uow_factory, FakeWorkspaceBackend(), review_backend=merge_backend)
      queue = WorkspaceMergeQueueService(uow_factory, service)
      lease = service.allocate("task_queue_fail", "session_1")
      patch = service.submit_patch(
        lease.lease_id,
        ArtifactRef("patch_fail_artifact", "artifact://patch/fail"),
        "will fail",
      )
      patch = PatchArtifact(
        patch_id="patch_fail",
        lease_id=patch.lease_id,
        task_id=patch.task_id,
        author_session_id=patch.author_session_id,
        artifact_ref=patch.artifact_ref,
        summary=patch.summary,
        status=patch.status,
        created_at=patch.created_at,
        updated_at=patch.updated_at,
      )
      with UnitOfWork(conn) as uow:
        uow.interactions.save_patch_artifact(patch)
      service.review("patch_fail", "reviewer", "approved")
      queue.enqueue("patch_fail")

      processed = queue.process_all("task_queue_fail")

      self.assertEqual(len(processed), 1)
      self.assertEqual(processed[0].status, "failed")
      self.assertIn("synthetic merge failure", processed[0].error)
    finally:
      conn.close()

  def test_merge_requires_approved_patch(self) -> None:
    conn = connect_sqlite()
    try:
      service = WorkspaceIsolationService(
        unit_of_work_factory(conn),
        FakeWorkspaceBackend(),
      )
      lease = service.allocate("task_1", "session_1")
      patch = service.submit_patch(
        lease.lease_id,
        ArtifactRef(artifact_id="patch_1", uri="artifact://patches/1"),
        summary="draft",
      )

      with self.assertRaises(ValueError):
        service.merge(patch.patch_id)
    finally:
      conn.close()

  @unittest.skipIf(shutil.which("git") is None, "git binary is required")
  def test_git_worktree_backend_allocates_commits_and_merges(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      repo = self._create_repo(Path(tmp) / "repo")
      worktrees = Path(tmp) / "worktrees"
      conn = connect_sqlite()
      try:
        backend = GitWorktreeBackend(repo, worktrees)
        service = WorkspaceIsolationService(unit_of_work_factory(conn), backend)

        lease = service.allocate("task_git", "session_git", base_ref="HEAD")
        workspace_path = self._path_from_uri(lease.workspace_uri)
        (workspace_path / "feature.txt").write_text("feature\n", encoding="utf-8")
        commit = backend.commit_all(lease.workspace_uri, "agent feature")
        patch = service.submit_patch(
          lease.lease_id,
          backend.create_patch_ref(lease.workspace_uri, artifact_id="git_patch_1"),
          summary="Add feature file.",
        )
        service.review(patch.patch_id, reviewer_id="reviewer", decision="approved")
        merged = service.merge(patch.patch_id)
        service.release(lease.lease_id)

        self.assertTrue(commit)
        self.assertEqual(merged.status, "merged")
        self.assertEqual(merged.artifact_ref.media_type, "application/vnd.agent-kernel.git-merge")
        self.assertEqual((repo / "feature.txt").read_text(encoding="utf-8"), "feature\n")
        self.assertFalse(workspace_path.exists())
      finally:
        conn.close()

  @unittest.skipIf(shutil.which("git") is None, "git binary is required")
  def test_git_worktree_backend_reports_merge_conflict(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      repo = self._create_repo(Path(tmp) / "repo")
      worktrees = Path(tmp) / "worktrees"
      conn = connect_sqlite()
      try:
        backend = GitWorktreeBackend(repo, worktrees)
        service = WorkspaceIsolationService(unit_of_work_factory(conn), backend)
        lease = service.allocate("task_conflict", "session_conflict", base_ref="HEAD")
        workspace_path = self._path_from_uri(lease.workspace_uri)
        (workspace_path / "README.md").write_text("from worktree\n", encoding="utf-8")
        backend.commit_all(lease.workspace_uri, "worktree edit")
        self._git(repo, ["checkout", "main"])
        (repo / "README.md").write_text("from main\n", encoding="utf-8")
        self._git(repo, ["add", "README.md"])
        self._git(repo, ["commit", "-m", "main edit"])
        patch = service.submit_patch(
          lease.lease_id,
          backend.create_patch_ref(lease.workspace_uri, artifact_id="git_patch_conflict"),
          summary="Conflicting readme edit.",
        )
        service.review(patch.patch_id, reviewer_id="reviewer", decision="approved")

        with self.assertRaisesRegex(RuntimeError, "merge failed"):
          service.merge(patch.patch_id)

        self.assertEqual(self._git(repo, ["status", "--porcelain"]), "")
      finally:
        conn.close()

  @staticmethod
  def _create_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    WorkspaceIsolationTests._git(path, ["init", "-b", "main"])
    WorkspaceIsolationTests._git(path, ["config", "user.email", "test@example.com"])
    WorkspaceIsolationTests._git(path, ["config", "user.name", "Test User"])
    (path / "README.md").write_text("base\n", encoding="utf-8")
    WorkspaceIsolationTests._git(path, ["add", "README.md"])
    WorkspaceIsolationTests._git(path, ["commit", "-m", "initial"])
    return path

  @staticmethod
  def _git(cwd: Path, args: list[str]) -> str:
    result = subprocess.run(
      ["git", *args],
      cwd=str(cwd),
      text=True,
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      check=False,
    )
    if result.returncode != 0:
      raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout

  @staticmethod
  def _path_from_uri(uri: str) -> Path:
    parsed = urlparse(uri)
    return Path(unquote(parsed.path))


class _RecordingMergeBackend:
  def __init__(self, fail_patch_id: str | None = None) -> None:
    self.fail_patch_id = fail_patch_id
    self.merged: list[PatchArtifact] = []

  def merge(self, patch: PatchArtifact) -> ArtifactRef:
    if patch.patch_id == self.fail_patch_id:
      raise RuntimeError("synthetic merge failure")
    self.merged.append(patch)
    return ArtifactRef(
      artifact_id=f"merged_{patch.patch_id}",
      uri=f"artifact://merged/{patch.patch_id}",
      media_type="application/vnd.agent-kernel.patch",
    )


if __name__ == "__main__":
  unittest.main()
