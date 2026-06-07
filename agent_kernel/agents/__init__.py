"""Agent orchestration package."""

from agent_kernel.agents.connectors import (
  AgentConnector,
  AgentConnectorRouter,
  ConnectorMessage,
  ConnectorRoute,
  ConnectorTurn,
  FakeAgentConnector,
  ProductCLIConnectorFactory,
  ProductCLIConnectorSpec,
  RoutedConnectorTurn,
  StdioAgentCommand,
  StructuredStdioAgentConnector,
)
from agent_kernel.agents.loop import AgentLoop, AgentTurnResult
from agent_kernel.agents.mailbox import build_mailbox_message
from agent_kernel.agents.group_chat import (
  DecisionArtifactService,
  CompositeSpeakerSelector,
  DeterministicDiscussionSummarizer,
  DiscussionSummarizer,
  FreeForAllSpeakerSelector,
  GroupChatService,
  ModeratorSpeakerSelector,
  RoundRobinSpeakerSelector,
  SpeakerSelection,
  SpeakerSelector,
)
from agent_kernel.agents.handoff import (
  ChannelMembershipHandoffPolicy,
  HandoffPolicy,
  HandoffService,
)
from agent_kernel.agents.interaction_fabric import InteractionFabric
from agent_kernel.agents.observer import ObserverService
from agent_kernel.agents.session import AgentSessionService, oneshot_agent_spec
from agent_kernel.agents.supervisor import SupervisorService
from agent_kernel.agents.taskboard import TaskBoardService
from agent_kernel.agents.team import AgentPoolScheduler
from agent_kernel.agents.workspace import (
  ArtifactMergeBackend,
  FakeWorkspaceBackend,
  PatchReviewBackend,
  WorkspaceBackend,
  WorkspaceIsolationService,
)

__all__ = [
  "AgentConnector",
  "AgentConnectorRouter",
  "AgentLoop",
  "AgentTurnResult",
  "AgentPoolScheduler",
  "AgentSessionService",
  "ConnectorMessage",
  "ConnectorRoute",
  "ConnectorTurn",
  "FakeAgentConnector",
  "FakeWorkspaceBackend",
  "DecisionArtifactService",
  "CompositeSpeakerSelector",
  "DeterministicDiscussionSummarizer",
  "DiscussionSummarizer",
  "FreeForAllSpeakerSelector",
  "GroupChatService",
  "ChannelMembershipHandoffPolicy",
  "HandoffPolicy",
  "HandoffService",
  "InteractionFabric",
  "ObserverService",
  "ProductCLIConnectorFactory",
  "ProductCLIConnectorSpec",
  "RoutedConnectorTurn",
  "ArtifactMergeBackend",
  "PatchReviewBackend",
  "ModeratorSpeakerSelector",
  "RoundRobinSpeakerSelector",
  "SpeakerSelection",
  "SpeakerSelector",
  "StdioAgentCommand",
  "StructuredStdioAgentConnector",
  "SupervisorService",
  "TaskBoardService",
  "WorkspaceBackend",
  "WorkspaceIsolationService",
  "build_mailbox_message",
  "oneshot_agent_spec",
]
