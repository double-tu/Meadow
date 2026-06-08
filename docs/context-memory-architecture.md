# Context and Memory Architecture

This document defines Meadow's phased target for agent context and memory
management. The goal is to let daily chat, workbenches, workflows, child
agents, CLI sessions, browser control, and future host surfaces share one
low-coupling context mechanism.

## Goals

- Keep model context small, explainable, and budgeted.
- Let the model see available capabilities without loading every detail.
- Store large or stale details outside the prompt as memories, events, or
  artifacts, and load them only when needed.
- Support long-running and asynchronous tasks without relying on one large
  conversation window.
- Make memory evolution pluggable so extraction, settlement, vector search,
  graph memory, or vendor stores can be swapped later.
- Keep host/UI code out of intent routing. The model should decide through
  skills, tool schemas, context, and memory.

## Seven Context Layers

### 1. System / Policy Context

Stable system rules, language preference, safety boundaries, grant state,
approval requirements, and side-effect policy. This layer is always loaded and
must stay short.

### 2. Agent Profile Context

The current agent identity, role, model capabilities, active model profile,
workspace, host surface, and default behavior. This layer lets different agents
use different models or policies without changing the rest of the pipeline.

### 3. Skill / Tool Index

A compact index of skills, workflows, MCP tools, atomic capabilities, agent
delegation, and workbench operations. The default prompt should include only
names, descriptions, when-to-use text, required grants, and schemas. Full skill
instructions, references, examples, scripts, or compiled workflow details are
loaded through explicit read/open tools.

### 4. Working Memory

The current objective, constraints, plan, active workbench/task/run IDs, pending
questions, unresolved decisions, and compact state. This layer is structured and
loaded on every turn.

### 5. Conversation Window

Recent user/assistant messages plus optional summaries of older turns. The raw
window is bounded. Older detail should be summarized, stored as episodic memory,
or referenced through artifacts.

### 6. Episodic / Event / Artifact Memory

Runtime events, tool calls, terminal output, browser page summaries, screenshots,
agent messages, workbench decisions, and large outputs. The prompt should carry
summaries and artifact refs, not raw payloads. Details are reloaded through
artifact/event read tools when needed.

### 7. Long-Term Semantic / Procedural Memory

User preferences, project facts, reusable workflows, known failures, learned
procedures, and stable decisions. This layer is retrieved by query and relevance,
not blindly injected.

## Core Interfaces

The architecture is centered around a high-level assembler and small provider
interfaces:

```text
ContextAssembler
  assemble(request: ContextAssemblyRequest) -> ContextPack

ContextLayerProvider
  collect(request, budget) -> ContextLayer

ContextBudgetManager
  allocate(max_tokens, layer_weights) -> LayerBudget
  trim(layers, max_tokens) -> ContextPack

MemoryStore / MemoryFacade
  read/write/retrieve working, episodic, semantic, procedural, artifact memory

ContextResourceReader
  read_memory(memory_id)
  search_memory(query)
  read_artifact(artifact_id)
  search_events(query)
  open_skill(skill_id)
  compact_context(run_id)
```

The assembler produces a `ModelContext` for existing model providers and writes
a `context.built` ledger event with selected/omitted layers, token allocation,
memory refs, artifact refs, and quality warnings.

## Data Flow

1. Host or runtime starts a model turn with user message, run ID, scope, agent
   ID, selected skills, tool schemas, and budget.
2. `ContextAssembler` asks each layer provider for candidates.
3. `ContextBudgetManager` allocates budget by layer and trims candidates by
   priority, relevance, freshness, and sensitivity.
4. `ContextPack` is rendered to `ModelContext`.
5. Model decides whether to answer, call tools, open a skill, read memory, read
   an artifact, request user input, delegate, or create/advance a workbench.
6. Tool results are compacted and artifactized when large.
7. A background curator may later extract facts, episodes, procedural memory, or
   skill evolution candidates from the event stream.

## Progressive Skill Disclosure

Skills should have three disclosure levels:

- `SkillCard`: always safe to index. Contains ID, name, description,
  when-to-use, grants, risk, and recommended tools.
- `SkillSpec`: loaded when selected or when the model calls `skill_open`.
  Contains instructions, constraints, failure modes, IO contract, examples, and
  workflow refs.
- `SkillResource`: loaded only during execution. Contains scripts, templates,
  long examples, reference files, or compiled workflow internals.

This keeps common turns cheap while preserving full procedural capability.

## Compression and Re-Expansion

Meadow should prefer reversible reduction:

- Keep raw large content in artifacts.
- Keep event payloads below the runtime limit.
- Replace old turns with summaries and refs.
- Keep omitted candidates in the context ledger.
- Provide model-visible read/search/open tools so omitted detail can be
  retrieved later.

Non-reversible summarization is acceptable only for low-risk conversational
history or after storing the original in an artifact/event/memory record.

## Memory Evolution

Memory evolution does not need to be fully automatic in the first phase, but the
pipeline must preserve enough hooks:

- every context build writes selected and omitted refs;
- every tool result can carry artifact refs;
- long task outputs become episodic memory candidates;
- high-confidence stable facts become semantic memory candidates;
- repeated successful procedures can become procedural memory or skills;
- conflict detection and settlement remain service-level policies, not model
  provider code.

## Integration Boundaries

- Daily chat and workbench runners call `ContextAssembler`; they do not hard
  route user intent.
- UI calls app services and displays context ledgers, artifacts, workbench state,
  and pending actions.
- Capability execution continues through `CapabilityRuntime` and policy grants.
- Model providers receive only `ModelContext`; provider adapters do not know
  about memory storage internals.
- Future vector DB, graph memory, browser DOM simplifiers, and CLI adapters plug
  in behind provider interfaces.

## Phase Plan

### Phase A - Core Assembly MVP

- Add domain models for context layer, context pack, and assembly request.
- Add `ContextBudgetManager`.
- Add `ContextAssembler` with default providers for system, agent profile, skill
  index, working memory, conversation, episodic/artifact refs, and semantic
  memory.
- Preserve existing `ContextManager` compatibility.
- Add tests for layer ordering, budget trimming, sensitive memory filtering,
  skill index disclosure, artifact refs, and ledger events.

### Phase B - Model-Visible Readers

- Add tools for skill open, memory search/read, event search, artifact read, and
  context compact/expand.
- Ensure tool outputs are compact and large content is artifactized.

Current MVP status:

- `skill_open` loads full `SkillCard` spec after the default compact index.
- `memory_search` and `memory_read` expose scoped memory lookup.
- `artifact_read` exposes artifact refs, metadata, and bounded content when a
  supported adapter can resolve the artifact.
- `event_search` exposes compact runtime event summaries.
- `context_compact` stores a supplied summary as episodic memory.
- `context_expand` expands memory, artifact, event, and skill refs in one call.

Artifact read has a bounded-content MVP: metadata inline
`content/body/text/payload` can be returned, and `file://` or local-path
artifacts can be read through the governed `FileWorkspace` adapter with
`start/count/keyword` windowing. Object storage, screenshot/blob, and multimedia
adapters are future work.

### Phase C - Runtime Integration

- Route daily chat and `ContinuousAgentRunner` through `ContextAssembler`.
- Use working memory for active run/workbench/task/pending state.
- Persist old conversation summaries when the raw window exceeds budget.

### Phase D - Background Curator

- Add asynchronous memory curator hooks.
- Extract episodic summaries, semantic facts, procedural candidates, and skill
  evolution candidates from run/event/tool streams.

Current MVP status:

- `ConversationHistoryCompactor` writes older chat turns as episodic memory and
  records compacted message IDs in chat session metadata.
- `MemoryCurator.curate_run` deterministically summarizes interesting run events
  into episodic memory.
- `MemoryCurator` reuses `MemoryEvolutionSettlementService` to settle candidate
  notes into semantic or procedural memory.
- Scheduler/worker/API integration is still future work.

### Phase E - Advanced Retrieval

- Add provider adapters for vector DB, graph memory, reranking, and conflict
  resolution.
- Add UI panels for context ledger, included/omitted memories, artifacts, and
  curator decisions.
