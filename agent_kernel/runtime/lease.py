"""Lease helpers for node step ownership."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from agent_kernel.domain.base import DomainModel, new_id, utc_now


@dataclass(slots=True)
class Lease(DomainModel):
  lease_id: str
  owner_id: str
  expires_at: datetime

  @classmethod
  def create(cls, owner_id: str, ttl_seconds: int = 60) -> "Lease":
    return cls(
      lease_id=new_id("lease"),
      owner_id=owner_id,
      expires_at=utc_now() + timedelta(seconds=ttl_seconds),
    )

  def is_expired(self, now: datetime | None = None) -> bool:
    return (now or utc_now()) >= self.expires_at

