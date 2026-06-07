"""Runtime factory helpers."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from threading import RLock

from agent_kernel.persistence import UnitOfWork


def unit_of_work_factory(conn: sqlite3.Connection) -> Callable[[], UnitOfWork]:
  lock = RLock()

  class LockedUnitOfWork(UnitOfWork):
    def __enter__(self) -> UnitOfWork:
      lock.acquire()
      try:
        return super().__enter__()
      except BaseException:
        lock.release()
        raise

    def __exit__(self, exc_type, exc, traceback) -> None:
      try:
        super().__exit__(exc_type, exc, traceback)
      finally:
        lock.release()

  return lambda: LockedUnitOfWork(conn)
