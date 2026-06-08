# Agent Kernel

Python MVP implementation of the Agent Kernel architecture described in `python-code-architecture-design.md`.

## Current Scope

Implemented MVP areas:

- Durable runtime with SQLite event log, state, checkpoint, retry, dead letter, budget, circuit breaker, idempotency basics, stale-step recovery scanner, recovery consistency checks, and conservative repair.
- Workflow graph execution with function, tool, and agent node executors.
- Agent orchestration with sessions, mailbox, child agent spawn/await/cancel, skill-aware turns, and agent-as-workflow-node.
- Capability runtime with policy checks, grant filesystem/network scope checks, approval flow, audit records, standard `ToolResult` envelopes, local/process tools, governed file/HTTP side-effect adapters, process stdout/stderr and structured JSONL streaming, timeout, injectable process isolation strategy, POSIX process group control, and tool-call cancel/kill control.
- Control workbench API for browser, desktop, and mobile control atoms with deterministic fake backend, GenericAgent-compatible browser-link HTTP backend, extension-backed tab creation/opening, browser tab/page-summary scan (`tabs_only`, title/url/feed/card/text extraction), Win32 desktop backend, UIA-style/UIAutomation desktop tree detectors, driver/HTTP vision detector adapters, and ADB mobile backend routed through capability policy/audit.
- Memory/context with seven-layer assembly, working/artifact memory, bounded artifact content reads, chat-history compaction, deterministic run-event curation, context budget, sensitivity filtering, tool visibility pruning, progressive Skill disclosure, model-visible context reader tools, large-memory artifact refs, and context ledger.
- Episodic memory with deterministic summarizer/retriever interfaces, conservative semantic consolidation, memory evolution candidate settlement, sparse/vector-store semantic retrieval interfaces, and structured fact conflict detection.
- Observability/replay with timeline, artifact inspect, cost ledger, audit sinks, exact/partial/recovery replay, and eval assertions.
- Extension manifest loader, contribution registry, permission-to-grant mapping, and dynamic importlib entrypoint runtime for registering tool providers.
- Autonomous exploration MVP with composable multi-strategy planning, dynamic tool/workflow composition, attempts, verification, failure reflection, trace distillation, draft workflow templates, and skill evolution records.
- Multi-agent interaction fabric MVP with channels, messages, round-robin/free-for-all/moderated group chat, agent pools, taskboard basics, observer findings with runtime pause/context correction/current-step interrupt, connector routing, and channel/cross-channel decision artifacts.
- Asynchronous agent delegation broker with parent-scoped delegate/status/cancel, depth limits, parent-run cancel cascade, orphaned-running recovery, large-result artifact handoff, long-poll status waits, connector cancellation, terminal reports, runtime events, HTTP endpoints, model-visible atomic tools, and a stdio MCP companion surface.
- Collaboration workbench API and model-visible tools for group chat, multi-CLI collaboration, technical review, and parallel child-agent delegation, backed by interaction channels, task slices, taskboard items, delegation broker calls, runtime events, desktop workspace aggregation, and Daily Agent tool-loop access.
- Workspace isolation interfaces with fake backend, Git worktree backend, and priority merge queue for isolated patch review/merge workflows.
- Handoff service with lineage, state summary, constraints, artifact refs, and channel message routing.
- Human intervention with event append, working-memory steering, CLI/HTTP `intervene`, pause-and-resume, and current-step interruption metadata.
- Skill service, compiled workflow registration/resolution, plan patch validation, and workflow patch application for controlled skill/workflow evolution.
- MCP stdio client/tool executor, generic Workbench protocol/fake/HTTP client, control Workbench, persistent agent connector protocol, structured stdio connector, product CLI shim profiles, product CLI connector factory, and multi-session connector router boundaries.
- MCP configuration management service with import/export, enable/agent-type filtering, stdio command assembly, HTTP/CLI management routes, and config-file import support.
- Scheduled task service with persisted one-shot/interval/simple-cron task definitions, due-task triggering, trigger history, and HTTP/CLI management routes.
- Control plane assembly and health/target inspection routes for configured browser-link, ADB mobile, Win32 desktop, or fake control backends.
- Desktop workspace API with workspace aggregation, global pending approvals, tool-call lists, event cursor streams, scheduled task mutation/history, control command execution, CORS support, and a thin React/Vite/Tauri shell scaffold.
- Desktop chat, skill management, and hot-updatable config center APIs with a Chinese conversation-first desktop shell, structured multi-provider/model/agent binding configuration, model capability tags, and a Codeg/AionUI-inspired settings workbench.
- Host DTOs plus HTTP host for task create, run inspect, run event NDJSON/SSE stream, artifact inspect, run cancel, human intervention, approval resolution, tool-call cancel/kill, and agent delegation requests.
- CLI host for sample run, inspect, replay, approve, reject, cancel, cancel/kill tool call, intervene, llm-smoke, and configured external-agent delegation.
- OpenAI-compatible LLM smoke command configured by environment variables or TOML/JSON config; desktop chat runtime can also resolve structured config-center providers for OpenAI-compatible, Gemini, and Anthropic model APIs.

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

Run a configured external-agent delegation through a product CLI connector:

```bash
python3 -m agent_kernel.hosts.cli --config agent-kernel.toml delegate-agent \
  --parent-run-id run_demo \
  --connector-id codex_cli \
  --agent-type codex \
  --task "Review the current repository and return the top risks."
```

`delegate-agent` waits for completion by default because the CLI process is short-lived. Use the HTTP host delegation endpoints for true background delegation in a long-lived process.

Expose delegation as MCP tools for an external agent CLI:

```bash
python3 -m agent_kernel.hosts.cli --config agent-kernel.toml mcp-delegation-server \
  --parent-run-id run_demo
```

The MCP server provides `delegate_to_agent`, `get_delegation_status`, and `cancel_delegation` over stdio JSON-RPC.

Create a multi-agent collaboration workbench through the HTTP host:

```bash
curl -X POST http://127.0.0.1:8080/collaboration/workbenches/parallel-delegation \
  -H 'Content-Type: application/json' \
  -d '{
    "title": "parallel search",
    "objective": "split a research task across multiple child agents",
    "auto_start": false,
    "members": [
      {"role":"docs-search","kind":"remote_agent","connector_id":"codex_cli","agent_type":"codex"},
      {"role":"web-search","kind":"remote_agent","connector_id":"claude_cli","agent_type":"claude"}
    ]
  }'
```

Use `auto_start=true` only when the HTTP host was started with a config file containing `agent_connectors`.

Recover orphaned running delegation records after a host restart:

```bash
python3 -m agent_kernel.hosts.cli --db /tmp/agent_kernel.sqlite recover-delegations
```

Settle memory evolution candidates from a completed run:

```bash
python3 -m agent_kernel.hosts.cli --db /tmp/agent_kernel.sqlite settle-memory run_demo --scope project_demo
```

Import MCP server definitions from config and inspect them:

```bash
python3 -m agent_kernel.hosts.cli --config agent-kernel.toml mcp-import
python3 -m agent_kernel.hosts.cli --db /tmp/agent_kernel.sqlite mcp-list --enabled-only
```

Create and trigger a scheduled task:

```bash
python3 -m agent_kernel.hosts.cli --db /tmp/agent_kernel.sqlite schedule-create \
  --name "daily repo check" \
  --kind every \
  --value 1h \
  --payload-json '{"title":"daily repo check","run_id":"run_daily_repo_check"}'
python3 -m agent_kernel.hosts.cli --db /tmp/agent_kernel.sqlite schedule-run-due
```

Inspect configured real-control backends:

```bash
python3 -m agent_kernel.hosts.cli --config agent-kernel.toml control-health
python3 -m agent_kernel.hosts.cli --config agent-kernel.toml control-health --targets --kind browser
```

Start, stop, restart, or inspect the desktop API + UI dev services:

```bash
python3 scripts/meadow_desktop.py start
python3 scripts/meadow_desktop.py status
python3 scripts/meadow_desktop.py restart
python3 scripts/meadow_desktop.py stop
```

By default this starts the API at `http://127.0.0.1:8083` and the React/Vite desktop shell at `http://127.0.0.1:4180`, with browser-link control configured for `http://127.0.0.1:18766/link`. Override ports or DB path when needed:

```bash
python3 scripts/meadow_desktop.py start --api-port 8080 --ui-port 4173 --db /tmp/meadow-desktop.sqlite
```

Install frontend dependencies once before starting the UI:

```bash
npm --prefix desktop install
```

The shell opens to the Chinese daily chat workspace by default. It also includes Skill 管理, 配置中心, 工作区, 审批, MCP, 计划任务, 控制台, and 事件流 views.

If installed as a package, the console script is:

```bash
agent-kernel --db /tmp/agent_kernel.sqlite sample-run --run-id run_demo --text hello
```

## Engineering Notes

- Runtime facts are persisted through event/state/checkpoint stores.
- Large payloads should be stored as artifacts and referenced from events.
- Side-effecting capabilities should be exposed as capabilities and executed through `CapabilityRuntime` and `PolicyEngine`; governed file/HTTP adapters and process tools prove this path for file writes, network access, and command execution.
- `ReplayService` does not re-execute model/tool calls.
- Current CLI is a minimal host, not the final Web/Desktop workspace.
- Browser control can use a GenericAgent-compatible `/link`-style HTTP backend. The neutral `BrowserLinkHTTPBackend` supports session listing, JavaScript execution, existing-tab navigation, no-target navigation through extension `tabs.create`, tab-only scans, and visible page summary extraction; desktop control can use the optional Win32 desktop backend plus UIA-style/UIAutomation/vision detectors, and mobile control can use the ADB backend plus optional vision detector.
- The desktop shell under `desktop/` is intentionally thin: it consumes workspace, approval, event, MCP, schedule, and control APIs and does not embed runtime or agent logic.
- Config center stores LLM settings as provider records, model records, capability flags, and agent bindings. Sensitive values are masked on read and preserved when the UI writes masked payloads back, so API keys do not get overwritten by `***`.

## Known MVP Gaps

- HTTP host has task creation backed by a default runtime workflow, JSON control endpoints for run cancel, human intervention, approval resolution, and tool-call cancel/kill plus NDJSON/SSE event streams; WebSocket streaming and the full Web/Desktop workspace are not implemented.
- Tool-call cancel/kill and process stream control only operate inside the current runtime process; after restart, host commands persist control requests but cannot signal or reattach to the original process group.
- Product CLI shim profiles for Codex/Claude/Gemini exist and run through a generic JSONL subprocess adapter; delegation can route through configured connectors, while deeper product-native protocol adapters and richer Cloud/Claude Code model-provider handoff remain future work.
- Collaboration workbench can create group chat, CLI collaboration, technical review, and parallel delegation objects through API and through model-selected Daily Agent tools. Rich desktop visualization, retry/delete/assign flows per task slice, long-running background auto-advance workers, real terminal stream viewing, large-scale search scheduling policy, and review merge policy remain future work.
- Handoff has a persistent service and channel routing; product CLI sessions can be connected through the generic JSONL shim profile path.
- Generic Workbench has a JSON HTTP client adapter. MCP has stdio JSON-RPC client/tool executor plus persisted config management. Control has config-driven browser-link HTTP, GenericAgent-style tab creation and page summary scan, Win32 desktop/UIA-style/UIAutomation/driver vision/HTTP vision, ADB mobile backends, and health/target inspection. Full `simphtml`-level visible DOM simplification, richer screenshot/interaction artifacts, product-specific Workbench adapters, and production-grade CV model packaging remain future work.
- Extension runtime can dynamically import entrypoints and register tool providers; sandboxed/plugin-process execution and richer contribution types remain future work.
- Recovery scanner can reschedule or dead-letter stale steps and conservatively repair missing/misaligned RunState checkpoint metadata, but complex artifact/event repair workflows are not implemented.
- Workspace isolation has protocol, fake-backed patch review workflow, Git worktree allocation/merge execution, and a priority merge queue; automatic conflict-resolution workflows and richer review policy are not implemented.
- Vector-store backed retrieval has protocol, in-memory adapter, and generic HTTP adapter coverage; vendor-native vector DB connectors, background long-term consolidation, and complex fact merge strategies remain future work.
- Advanced dynamic workflow/tool optimization, branching composition, and learned composition policies remain future work; deterministic linear tool/workflow composition is implemented.
