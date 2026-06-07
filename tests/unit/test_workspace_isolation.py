import unittest

from agent_kernel.agents import FakeWorkspaceBackend, WorkspaceIsolationService
from agent_kernel.domain import ArtifactRef
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


if __name__ == "__main__":
  unittest.main()
