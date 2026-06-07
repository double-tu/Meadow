"""Domain-level exceptions."""


class DomainError(Exception):
  """Base exception for domain validation failures."""


class DomainValidationError(DomainError):
  """Raised when a domain object violates invariants."""


class InvalidStateTransitionError(DomainValidationError):
  """Raised when a state transition is not allowed."""

