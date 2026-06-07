"""State enums and transition guards."""

from enum import StrEnum
from typing import TypeVar

from agent_kernel.domain.errors import InvalidStateTransitionError


class RunStatus(StrEnum):
  PENDING = "pending"
  RUNNING = "running"
  PAUSED = "paused"
  INTERRUPTED = "interrupted"
  FAILED = "failed"
  COMPLETED = "completed"
  CANCELLED = "cancelled"


class NodeStepStatus(StrEnum):
  SCHEDULED = "scheduled"
  LEASED = "leased"
  RUNNING = "running"
  SUCCEEDED = "succeeded"
  RETRY_WAIT = "retry_wait"
  FAILED = "failed"
  CANCELLED = "cancelled"
  INTERRUPTED = "interrupted"


class TaskStatus(StrEnum):
  CREATED = "created"
  READY = "ready"
  CLAIMED = "claimed"
  RUNNING = "running"
  BLOCKED = "blocked"
  REVIEW = "review"
  DONE = "done"
  FAILED = "failed"
  CANCELLED = "cancelled"


class AgentStatus(StrEnum):
  DEFINED = "defined"
  STARTING = "starting"
  RUNNING = "running"
  WAITING_INPUT = "waiting_input"
  IDLE = "idle"
  COMPLETED = "completed"
  FAILED = "failed"
  CANCELLED = "cancelled"


class ApprovalStatus(StrEnum):
  REQUESTED = "requested"
  APPROVED = "approved"
  REJECTED = "rejected"
  EXPIRED = "expired"
  CANCELLED = "cancelled"


class ToolCallStatus(StrEnum):
  REQUESTED = "requested"
  AWAITING_APPROVAL = "awaiting_approval"
  APPROVED = "approved"
  RUNNING = "running"
  PAUSING = "pausing"
  PAUSED = "paused"
  CANCELLING = "cancelling"
  CANCELLED = "cancelled"
  KILLING = "killing"
  KILLED = "killed"
  SUCCEEDED = "succeeded"
  FAILED = "failed"


class HumanInterventionStatus(StrEnum):
  CREATED = "created"
  APPLIED = "applied"
  SUPERSEDED = "superseded"
  REJECTED = "rejected"


class RecoveryStatus(StrEnum):
  PENDING = "pending"
  RUNNING = "running"
  SUCCEEDED = "succeeded"
  FAILED = "failed"
  DEAD_LETTERED = "dead_lettered"


class CircuitStatus(StrEnum):
  CLOSED = "closed"
  OPEN = "open"
  HALF_OPEN = "half_open"


StateT = TypeVar("StateT", bound=StrEnum)

RUN_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
  RunStatus.PENDING: {RunStatus.RUNNING},
  RunStatus.RUNNING: {
    RunStatus.PAUSED,
    RunStatus.INTERRUPTED,
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
  },
  RunStatus.PAUSED: {RunStatus.RUNNING, RunStatus.CANCELLED},
  RunStatus.INTERRUPTED: {RunStatus.RUNNING, RunStatus.CANCELLED},
  RunStatus.FAILED: {RunStatus.RUNNING},
  RunStatus.COMPLETED: set(),
  RunStatus.CANCELLED: set(),
}

NODE_STEP_TRANSITIONS: dict[NodeStepStatus, set[NodeStepStatus]] = {
  NodeStepStatus.SCHEDULED: {NodeStepStatus.LEASED},
  NodeStepStatus.LEASED: {NodeStepStatus.RUNNING},
  NodeStepStatus.RUNNING: {
    NodeStepStatus.SUCCEEDED,
    NodeStepStatus.RETRY_WAIT,
    NodeStepStatus.FAILED,
    NodeStepStatus.CANCELLED,
    NodeStepStatus.INTERRUPTED,
  },
  NodeStepStatus.RETRY_WAIT: {NodeStepStatus.SCHEDULED},
  NodeStepStatus.SUCCEEDED: set(),
  NodeStepStatus.FAILED: set(),
  NodeStepStatus.CANCELLED: set(),
  NodeStepStatus.INTERRUPTED: {NodeStepStatus.SCHEDULED, NodeStepStatus.CANCELLED},
}

TASK_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
  TaskStatus.CREATED: {TaskStatus.READY},
  TaskStatus.READY: {TaskStatus.CLAIMED},
  TaskStatus.CLAIMED: {TaskStatus.RUNNING},
  TaskStatus.RUNNING: {
    TaskStatus.BLOCKED,
    TaskStatus.REVIEW,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
  },
  TaskStatus.BLOCKED: {TaskStatus.READY},
  TaskStatus.REVIEW: {TaskStatus.DONE, TaskStatus.READY},
  TaskStatus.FAILED: {TaskStatus.READY},
  TaskStatus.DONE: set(),
  TaskStatus.CANCELLED: set(),
}

AGENT_TRANSITIONS: dict[AgentStatus, set[AgentStatus]] = {
  AgentStatus.DEFINED: {AgentStatus.STARTING},
  AgentStatus.STARTING: {AgentStatus.RUNNING, AgentStatus.CANCELLED, AgentStatus.FAILED},
  AgentStatus.RUNNING: {
    AgentStatus.WAITING_INPUT,
    AgentStatus.IDLE,
    AgentStatus.COMPLETED,
    AgentStatus.FAILED,
    AgentStatus.CANCELLED,
  },
  AgentStatus.WAITING_INPUT: {AgentStatus.RUNNING, AgentStatus.CANCELLED},
  AgentStatus.IDLE: {AgentStatus.RUNNING, AgentStatus.CANCELLED},
  AgentStatus.COMPLETED: set(),
  AgentStatus.FAILED: set(),
  AgentStatus.CANCELLED: set(),
}

APPROVAL_TRANSITIONS: dict[ApprovalStatus, set[ApprovalStatus]] = {
  ApprovalStatus.REQUESTED: {
    ApprovalStatus.APPROVED,
    ApprovalStatus.REJECTED,
    ApprovalStatus.EXPIRED,
    ApprovalStatus.CANCELLED,
  },
  ApprovalStatus.APPROVED: set(),
  ApprovalStatus.REJECTED: set(),
  ApprovalStatus.EXPIRED: set(),
  ApprovalStatus.CANCELLED: set(),
}

TOOL_CALL_TRANSITIONS: dict[ToolCallStatus, set[ToolCallStatus]] = {
  ToolCallStatus.REQUESTED: {ToolCallStatus.AWAITING_APPROVAL, ToolCallStatus.APPROVED},
  ToolCallStatus.AWAITING_APPROVAL: {ToolCallStatus.APPROVED, ToolCallStatus.CANCELLED},
  ToolCallStatus.APPROVED: {ToolCallStatus.RUNNING},
  ToolCallStatus.RUNNING: {
    ToolCallStatus.PAUSING,
    ToolCallStatus.CANCELLING,
    ToolCallStatus.KILLING,
    ToolCallStatus.SUCCEEDED,
    ToolCallStatus.FAILED,
  },
  ToolCallStatus.PAUSING: {ToolCallStatus.PAUSED, ToolCallStatus.CANCELLING},
  ToolCallStatus.PAUSED: {ToolCallStatus.RUNNING, ToolCallStatus.CANCELLING},
  ToolCallStatus.CANCELLING: {ToolCallStatus.CANCELLED, ToolCallStatus.KILLING},
  ToolCallStatus.KILLING: {ToolCallStatus.KILLED},
  ToolCallStatus.CANCELLED: set(),
  ToolCallStatus.KILLED: set(),
  ToolCallStatus.SUCCEEDED: set(),
  ToolCallStatus.FAILED: set(),
}

HUMAN_INTERVENTION_TRANSITIONS: dict[HumanInterventionStatus, set[HumanInterventionStatus]] = {
  HumanInterventionStatus.CREATED: {
    HumanInterventionStatus.APPLIED,
    HumanInterventionStatus.SUPERSEDED,
    HumanInterventionStatus.REJECTED,
  },
  HumanInterventionStatus.APPLIED: set(),
  HumanInterventionStatus.SUPERSEDED: set(),
  HumanInterventionStatus.REJECTED: set(),
}

RECOVERY_TRANSITIONS: dict[RecoveryStatus, set[RecoveryStatus]] = {
  RecoveryStatus.PENDING: {RecoveryStatus.RUNNING},
  RecoveryStatus.RUNNING: {
    RecoveryStatus.SUCCEEDED,
    RecoveryStatus.FAILED,
    RecoveryStatus.DEAD_LETTERED,
  },
  RecoveryStatus.FAILED: {RecoveryStatus.PENDING},
  RecoveryStatus.SUCCEEDED: set(),
  RecoveryStatus.DEAD_LETTERED: set(),
}

CIRCUIT_TRANSITIONS: dict[CircuitStatus, set[CircuitStatus]] = {
  CircuitStatus.CLOSED: {CircuitStatus.OPEN},
  CircuitStatus.OPEN: {CircuitStatus.HALF_OPEN},
  CircuitStatus.HALF_OPEN: {CircuitStatus.CLOSED, CircuitStatus.OPEN},
}

TRANSITIONS: dict[type[StrEnum], dict[StrEnum, set[StrEnum]]] = {
  RunStatus: RUN_TRANSITIONS,
  NodeStepStatus: NODE_STEP_TRANSITIONS,
  TaskStatus: TASK_TRANSITIONS,
  AgentStatus: AGENT_TRANSITIONS,
  ApprovalStatus: APPROVAL_TRANSITIONS,
  ToolCallStatus: TOOL_CALL_TRANSITIONS,
  HumanInterventionStatus: HUMAN_INTERVENTION_TRANSITIONS,
  RecoveryStatus: RECOVERY_TRANSITIONS,
  CircuitStatus: CIRCUIT_TRANSITIONS,
}


def can_transition(current: StateT, target: StateT) -> bool:
  if type(current) is not type(target):
    return False
  transition_map = TRANSITIONS.get(type(current))
  if transition_map is None:
    raise ValueError(f"No transition map registered for {type(current).__name__}.")
  return target in transition_map[current]


def assert_transition(current: StateT, target: StateT) -> None:
  if not can_transition(current, target):
    raise InvalidStateTransitionError(
      f"Invalid {type(current).__name__} transition: {current.value} -> {target.value}"
    )
