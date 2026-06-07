# Agent Kernel

Python MVP implementation of the Agent Kernel architecture described in `python-code-architecture-design.md`.

## Current Scope

Implemented MVP areas:

- Durable runtime with SQLite event log, state, checkpoint, retry, dead letter, budget, circuit breaker, idempotency basics, stale-step recovery scanner, recovery consistency checks, and conservative repair.
- Workflow graph execution with function, tool, and agent node executors.
- Agent orchestration with sessions, mailbox, child agent spawn/await/cancel, skill-aware turns, and agent-as-workflow-node.
- Capability runtime with policy checks, approval flow, audit records, local/process tools, process stdout/stderr streaming, timeout, injectable process isolation strategy, POSIX process group control, and tool-call cancel/kill control.
- Control workbench API for browser, desktop, and mobile control atoms with deterministic fake backend, TMWebDriver HTTP browser backend, Win32 desktop backend, UIA-style desktop tree detector, and ADB mobile backend routed through capability policy/audit.
- Memory/context with working/artifact memory, context budget, sensitivity filtering, tool visibility pruning, large-memory artifact refs, and context ledger.
- Episodic memory with deterministic summarizer/retriever interfaces, conservative semantic consolidation, sparse semantic retrieval, and structured fact conflict detection.
- Observability/replay with timeline, artifact inspect, cost ledger, exact/partial/recovery replay, and eval assertions.
- Extension manifest loader, contribution registry, and permission-to-grant mapping.
- Autonomous exploration MVP with composable multi-strategy planning, attempts, verification, failure reflection, trace distillation, draft workflow templates, and skill evolution records.
- Multi-agent interaction fabric MVP with channels, messages, round-robin/free-for-all/moderated group chat, agent pools, taskboard basics, observer findings with runtime pause control, connector routing, and channel/cross-channel decision artifacts.
- Workspace isolation interfaces with fake backend and Git worktree backend for isolated patch review/merge workflows.
- Handoff service with lineage, state summary, constraints, artifact refs, and channel message routing.
- Human intervention with event append, working-memory steering, and CLI `intervene`.
- Skill service, compiled workflow registration/resolution, plan patch validation, and workflow patch application for controlled skill/workflow evolution.
- MCP stdio client/tool executor, generic Workbench protocol/fake/HTTP client, control Workbench, persistent agent connector protocol, structured stdio connector, product CLI shim profiles, product CLI connector factory, and multi-session connector router boundaries.
- Host DTOs plus HTTP host for task create, run inspect, run event NDJSON/SSE stream, artifact inspect, run cancel, human intervention, approval resolution, and tool-call cancel/kill requests.
- CLI host for sample run, inspect, replay, approve, reject, cancel, cancel/kill tool call, intervene, and llm-smoke.
- OpenAI-compatible LLM smoke command configured by environment variables or TOML/JSON config.

## Quick Start

Run tests:

```bash
python3 -m unittest discover -s tests
```

Run a sample workflow:

```bash
python3 -m agent_kernel.hosts.cli --db /tmp/agent_kernel.sqlite sample-run --run-id run_demo --text hello
```

Inspect timeline:

```bash
python3 -m agent_kernel.hosts.cli --db /tmp/agent_kernel.sqlite inspect run_demo
```

Replay:

```bash
python3 -m agent_kernel.hosts.cli --db /tmp/agent_kernel.sqlite replay run_demo
```

Run a real LLM smoke call with an OpenAI-compatible endpoint:

```bash
export AGENT_KERNEL_LLM_MODEL="gpt-4.1-mini"
export AGENT_KERNEL_LLM_API_KEY="your-api-key"
export AGENT_KERNEL_LLM_BASE_URL="https://api.openai.com/v1"
python3 -m agent_kernel.hosts.cli llm-smoke --prompt "Say hello in one sentence."
```

The environment loader also accepts `OPENAI_MODEL`, `OPENAI_API_KEY`, and `OPENAI_BASE_URL`.

Or use a config file:

```toml
[llm]
provider = "openai-compatible"
model = "gpt-4.1-mini"
base_url = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"
timeout_seconds = 60
```

Then run:

```bash
export OPENAI_API_KEY="your-api-key"
python3 -m agent_kernel.hosts.cli --config agent-kernel.toml llm-smoke --prompt "Say hello."
```

If installed as a package, the console script is:

```bash
agent-kernel --db /tmp/agent_kernel.sqlite sample-run --run-id run_demo --text hello
```

## Engineering Notes

- Runtime facts are persisted through event/state/checkpoint stores.
- Large payloads should be stored as artifacts and referenced from events.
- All side-effecting capabilities go through `CapabilityRuntime` and `PolicyEngine`.
- `ReplayService` does not re-execute model/tool calls.
- Current CLI is a minimal host, not the final Web/Desktop workspace.
- Browser control can use the TMWebDriver-compatible HTTP backend, desktop control can use the optional Win32 desktop backend plus UIA-style tree detector, and mobile control can use the ADB backend.

## Known MVP Gaps

- HTTP host has task creation backed by a default runtime workflow, JSON control endpoints for run cancel, human intervention, approval resolution, and tool-call cancel/kill plus NDJSON/SSE event streams; WebSocket streaming and the full Web/Desktop workspace are not implemented.
- Tool-call cancel/kill and process stream control only operate inside the current runtime process; after restart, host commands persist control requests but cannot signal or reattach to the original process group.
- Product CLI shim profiles for Codex/Claude/Gemini exist and run through a generic JSONL subprocess adapter; deeper product-native protocol adapters remain future work.
- Handoff has a persistent service and channel routing; product CLI sessions can be connected through the generic JSONL shim profile path.
- Generic Workbench has a JSON HTTP client adapter. MCP has a stdio JSON-RPC client/tool executor; control has TMWebDriver HTTP browser, Win32 desktop/UIA-style, and ADB mobile backends. Product-specific Workbench adapters remain future work.
- Recovery scanner can reschedule or dead-letter stale steps and conservatively repair missing/misaligned RunState checkpoint metadata, but complex artifact/event repair workflows are not implemented.
- Workspace isolation has protocol, fake-backed patch review workflow, and Git worktree allocation/merge execution; advanced multi-agent merge queues and conflict-resolution workflows are not implemented.
- External vector-store backed retrieval and background long-term consolidation are not implemented; local sparse semantic retrieval and structured fact conflict detection are available.
- Advanced dynamic workflow/tool composition remains future work.
