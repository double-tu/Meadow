# Agent 架构设计报告 v3

更新时间: 2026-06-03

本版目标: 在 v2 调研和架构判断基础上, 收敛出一套可实现、可演进、足够抽象但不过度空泛的 Agent Kernel 架构。v3 不再重复展开每个开源项目的逐项分析, 而是把 v2 的结论转化为目标架构、模块边界、运行时协议、扩展策略和演进路线。

---

## 0. v3 变更摘要

v3 相比 v2 的核心变化如下。

1. **从调研报告转为目标架构设计**  
   v2 的重点是比较项目和提炼原则。v3 的重点是形成可落地的架构蓝图, 用于后续拆分模块、定义接口、设计 MVP 和长期演进。

2. **补上 `model.gateway` 作为核心模块**  
   v2 在 MVP 清单中提到 Model Gateway, 但没有放入核心模块层。v3 将其提升为一等模块, 负责模型 Provider、流式输出、结构化输出、工具调用协议、成本、限流、fallback 和模型能力声明。

3. **区分 `ToolAdapter` 与 `AgentConnector`**  
   v2 倾向把 Agent、CLI、Workflow 都包装为 Tool。v3 保留“统一可调用”的思想, 但把外部 Agent 互操作从工具系统中拆出。Agent 可以被包装成 Node 或 Tool-like capability, 但底层协议必须保留多轮状态、任务、消息、artifact、取消、流式进度等 agent 语义。

4. **细化 “一切都是 Node”**  
   v3 将原则修正为: 一切可执行能力都应是 `Node-compatible executable`, 而不是所有对象都继承同一个 Node 类型。这样既保留递归组合能力, 也避免 Agent、Tool、Workflow、Human、Memory 被迫耦合成一个巨型接口。

5. **引入生产级调度语义**  
   v2 已有 checkpoint、retry、cancel。v3 进一步加入 lease、heartbeat、idempotency key、dead-letter、compensation、concurrency limit、backpressure、timeout budget 和 cancellation cascade。

6. **安全从“工具权限”升级为“能力租约”**  
   v3 引入 `CapabilityGrant` 思想, 将权限绑定到 agent、task、tool call、workspace、secret、network、time window 和 approval policy, 避免长期过宽授权。

7. **事件溯源改为混合事件溯源**  
   v2 强调 Event Sourcing。v3 进一步明确: 事件日志保存事实、因果关系和引用; 大 payload、文件、diff、模型 transcript、图片、长日志进入 Artifact Store, 事件只保存引用。

8. **补上评测与回放系统**  
   复杂 Agent 没有 eval/replay/regression harness 就无法长期演进。v3 增加 `evaluation.replay` 模块, 支持 golden trace、工具 mock、memory recall 测试、workflow replay 和成本回归。

---

## 1. 设计目标与非目标

### 1.1 设计目标

目标是构建一个可作为长期基座的 Agent Kernel, 支持:

- 复杂工作流组合: chain、DAG、循环、条件、并行、子工作流、动态路由、人类介入。
- 多代理协作: supervisor-worker、group chat、pipeline、blackboard/taskboard、异步 delegation。
- 子代理使用: oneshot、persistent、team member、remote agent、external CLI agent。
- 工具调用: 本地函数、CLI、HTTP、SDK、MCP、浏览器、数据库、代码执行、其他 agent。
- 记忆系统: working、episodic、semantic、procedural、artifact 五层记忆。
- 上下文管理: 检索、排序、压缩、摘要、token budget、context ledger。
- 长任务推进: checkpoint、resume、pause、cancel、retry、interrupt、human approval、任务板持续推进。
- 外部集成: MCP、A2A-like remote agent protocol、OpenAI Agents SDK-style handoff、LangGraph-like subgraph、CrewAI-like multi-agent process。
- 工程治理: 事件日志、审计、权限、沙箱、trace、成本、评测、回放、插件扩展。

### 1.2 非目标

第一阶段不追求:

- 一开始就实现完整 marketplace。
- 一开始就做复杂视觉化编排器。
- 一开始就支持企业级多租户和复杂 RBAC/ABAC。
- 一开始就实现完全自治的 agent 社会结构。
- 一开始就做自动优化 planner 或 self-improvement。
- 一开始就把所有 memory 类型做成复杂智能系统。

第一阶段应该把 runtime、state、event、tool、model、workflow、agent lifecycle 做稳。

---

## 2. 参考项目的架构吸收

v3 仍保留 v2 的项目吸收策略, 但加入几个补充参考方向。

| 能力域 | 主要参考 | 吸收内容 |
|---|---|---|
| Workflow Kernel | LangGraph | typed state、graph、subgraph、checkpoint、interrupt、Command/Send |
| Event Multi-Agent Runtime | AutoGen Core、Microsoft Agent Framework | actor/event、topic/subscription、agent workflow、typed routing |
| Multi-Agent App Model | CrewAI | crew/agent/task/flow 分层、角色协作、统一记忆 |
| External Agent SDK | OpenAI Agents SDK | agents、tools、handoffs、guardrails、sessions、tracing |
| Subagent Delegation | codeg、SpectrAI、ClaudeCodeRev 样本 | spawn、await、cancel、lineage、worktree/sandbox、异步任务 |
| Memory | GenericAgent、CrewAI、desktop-cc-gui | 分层记忆、recall flow、context ledger、project memory |
| Extension | AionUi、LangChain ecosystem | manifest、权限声明、provider/tool/memory/checkpointer 扩展 |
| Workflow DX | Mastra、CrewAI Flow | schema-first workflow、workflow-as-step、可视化与调试 |
| Tool Interop | MCP | tool/resource/prompt 外部能力标准化 |
| Remote Agent Interop | A2A-like protocol | agent card、task、message、artifact、streaming/polling |

核心判断不变: 没有任何一个项目可以直接完整复制。最优路径是分层吸收。

---

## 3. 架构总原则

### 原则 1: Node-compatible, 不等于同一个 Node 类

Agent、Tool、Workflow、Human、Memory 操作、Evaluation、Code Execution 都应该可以被放入 Workflow Graph 中执行。但它们不应被迫实现同一个厚接口。

更合理的拆法:

- `NodeSpec`: 声明式节点定义。
- `NodeExecutor`: 节点执行器。
- `CapabilityRef`: 指向 Tool、Agent、Workflow、Model、Memory Operation 的引用。
- `ExecutionCommand`: 节点执行后返回的路由、等待、中断、完成等命令。

这样可以保留“一切可组合”的能力, 同时保持每类能力自身的协议完整。

### 原则 2: Workflow 是显式图, Agent Loop 是内建工作流

Agent 不是一个魔法类。一个 Agent 的内部循环可以视为预定义工作流:

```text
load_state
  -> build_context
  -> call_model
  -> route_tool_or_finish
  -> execute_capability
  -> persist_turn
  -> update_memory
  -> continue_or_stop
```

这使得 Agent 可以被调试、回放、插入 human approval、替换 context strategy 或 memory strategy。

### 原则 3: Runtime 先于 Intelligence

复杂 Agent 的成败主要取决于 harness:

- 状态是否准确。
- 工具是否可靠。
- 权限是否收敛。
- 失败是否可恢复。
- 上下文是否可解释。
- 子任务是否可追踪。
- 成本是否可观测。

Planner、reflection、self-improvement 是上层能力, 不应污染底层 runtime。

### 原则 4: Memory 与 Context 严格分离

Memory 是长期存储层。Context 是本轮模型调用的输入装配层。

二者之间只通过 `RetrievalPack`、`ArtifactRef`、`MemoryRef`、`ContextLedgerEntry` 对接。

### 原则 5: 所有外部能力都通过 Capability Runtime

外部能力包括:

- Tool
- CLI
- MCP server
- HTTP API
- SDK
- Local/remote agent
- Workflow
- Human

上层只看到 capability, 底层保留具体协议差异。

### 原则 6: 默认持久化, 默认可回放, 默认可审计

每次 run、node step、tool call、model call、memory write、permission decision、human decision 都必须产生事件或审计记录。

### 原则 7: 默认最小权限

Agent 拿到的是短期能力租约, 不是永久权限。权限应绑定任务、工作区、工具、时间窗口和审批策略。

### 原则 8: 扩展优先通过 manifest, 内核优先通过协议

内核不应为了某个插件或 Provider 改代码。扩展通过 manifest 声明能力, 通过协议注册执行器、模型、工具、memory backend、checkpointer、UI panel 和 trace sink。

---

## 4. 总体架构

```text
┌──────────────────────────────────────────────────────────────┐
│                         Host Layer                           │
│  CLI / Web UI / Desktop / HTTP API / Scheduler / Remote API  │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                    Conversation & Task Hub                   │
│  Thread / Turn / Objective / Task / Dependency / Progress    │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                         Kernel Runtime                       │
│  Run Scheduler / Event Store / State Store / Checkpointer    │
│  Lease / Retry / Timeout / Cancel / Interrupt / Policy Hooks │
└──────────────────────────────────────────────────────────────┘
                 │                    │                    │
                 ▼                    ▼                    ▼
┌────────────────────────┐ ┌────────────────────┐ ┌────────────────────┐
│     Workflow Graph     │ │    Agent Runtime   │ │   Model Gateway    │
│ NodeSpec / Executor    │ │ Session / Mailbox  │ │ Provider / Stream  │
│ Edge / Command / State │ │ TaskBoard / Team   │ │ Tool-call / Cost   │
└────────────────────────┘ └────────────────────┘ └────────────────────┘
                 │                    │                    │
                 ▼                    ▼                    ▼
┌────────────────────────┐ ┌────────────────────┐ ┌────────────────────┐
│   Capability Runtime   │ │  Memory System     │ │  Context Manager   │
│ Tool / Workbench / MCP │ │ WM / EM / SM / PM  │ │ Retrieval Pack     │
│ CLI / HTTP / AgentConn │ │ Artifact Memory    │ │ Budget / Compress  │
└────────────────────────┘ └────────────────────┘ └────────────────────┘
                 │                    │                    │
                 └──────────────┬─────┴────────────┬──────┘
                                ▼                  ▼
                 ┌────────────────────┐ ┌────────────────────┐
                 │ Persistence Layer  │ │ Observability      │
                 │ SQL / KV / Vector  │ │ Trace / Eval       │
                 │ Blob / Event Log   │ │ Cost / Audit       │
                 └────────────────────┘ └────────────────────┘
                                │
                                ▼
                 ┌────────────────────────────────────────────┐
                 │             Extension SDK                  │
                 │ Manifest / Permissions / Plugins / UI      │
                 └────────────────────────────────────────────┘
```

---

## 5. 核心模块设计

### 5.1 `kernel.runtime`

职责:

- run lifecycle
- scheduling
- durable execution
- checkpoint
- event append
- retry / timeout / cancel
- interrupt / resume
- lease / heartbeat
- policy hook
- dispatch node execution

关键设计:

- 所有 run 都有 `run_id`。
- 所有 step 都有 `step_id`、`attempt`、`idempotency_key`。
- 所有异步执行都需要 lease, 防止重复 worker 执行同一个 step。
- retry 必须区分 deterministic failure、transient failure、policy failure、model failure。
- cancel 必须支持 cascade, 从 workflow 到 child task、agent session、tool call。

### 5.2 `workflow.graph`

职责:

- workflow definition
- node registry
- edge routing
- state reducer
- subworkflow
- dynamic route
- fan-out / fan-in
- loop
- compensation
- human interrupt

核心对象:

- `WorkflowSpec`
- `NodeSpec`
- `EdgeSpec`
- `NodeExecutor`
- `GraphState`
- `Reducer`
- `ExecutionCommand`
- `SubWorkflowRef`

支持的执行形态:

- linear chain
- DAG
- conditional branch
- router
- loop
- parallel map
- join
- subworkflow
- supervisor-worker workflow
- taskboard workflow
- human approval workflow

重要约束:

- 节点输入输出必须有 schema。
- 节点不能直接写全局状态, 只能返回 patch。
- 并发 patch 通过 reducer 合并。
- 节点可以返回 command, 但不应直接操纵 scheduler。

### 5.3 `agent.runtime`

职责:

- agent definition
- agent session lifecycle
- child agent spawn
- handoff
- mailbox
- taskboard
- group/team orchestration
- workspace isolation
- agent lineage

Agent 生命周期:

```text
defined -> starting -> running -> waiting_input -> idle
        -> completed
        -> failed
        -> cancelled
```

Agent 类型:

- `oneshot`: 接收任务, 产生结果, 生命周期短。
- `persistent`: 具备持续会话和上下文。
- `team_member`: 在团队或任务板中长期存在。
- `remote`: 通过 AgentConnector 接入外部 agent。
- `human_proxy`: 人类作为 agent-like participant。

协作模式:

- Supervisor-Worker
- Group Chat
- Pipeline
- Blackboard / TaskBoard
- Handoff
- Swarm-like routing

### 5.4 `capability.runtime`

职责:

- tool registry
- workbench
- MCP client/server bridge
- CLI adapter
- HTTP adapter
- SDK adapter
- browser/code execution adapter
- workflow adapter
- agent connector
- result normalization

v3 的关键变化是拆分:

```text
Capability Runtime
├── Tool Runtime
│   ├── Local Tool
│   ├── CLI Tool
│   ├── HTTP Tool
│   ├── SDK Tool
│   └── MCP Tool
├── Workflow Adapter
│   └── registered workflow as callable capability
└── Agent Connector
    ├── local agent connector
    ├── CLI agent connector
    ├── remote agent connector
    └── A2A-like connector
```

AgentConnector 不只是 Tool, 它需要保留:

- multi-turn session
- task status
- artifact
- progress event
- cancellation
- streaming
- mailbox
- capability discovery

### 5.5 `model.gateway`

职责:

- provider abstraction
- chat/completion/responses adapter
- streaming normalization
- tool-call normalization
- structured output enforcement
- model capability registry
- rate limit
- fallback
- cost accounting
- prompt/message format conversion
- safety/model policy hook

模型能力声明应包含:

- context window
- supports tools
- supports parallel tool calls
- supports structured output
- supports multimodal input
- supports image/audio output
- supports reasoning traces
- streaming capability
- cost model
- latency profile

`model.gateway` 不负责长期记忆, 不负责工具执行, 不负责 workflow routing。它只负责把模型调用变成可治理、可观测、可替换的 runtime capability。

### 5.6 `memory.system`

职责:

- memory write policy
- memory retrieval
- memory compaction
- retention/forgetting
- conflict resolution
- memory provenance
- memory indexing

五层记忆:

- Working Memory: 当前目标、约束、待办、已验证事实。
- Episodic Memory: 历史任务、会话摘要、关键事件、工具轨迹。
- Semantic Memory: 稳定事实、用户偏好、项目知识、团队约定。
- Procedural Memory: SOP、skills、workflow templates、repair recipes。
- Artifact Memory: 文件、diff、报告、图片、测试结果、中间数据。

MemoryItem 必须带元数据:

- scope
- source event ids
- source artifact refs
- confidence
- importance
- sensitivity
- ttl
- version
- embedding model
- created by
- verified by
- last used at

写入原则:

- 不允许所有 turn 自动写入长期记忆。
- memory write 需要 policy 或后台 summarizer。
- 高敏感内容默认不进入 semantic memory。
- 冲突记忆不能静默覆盖, 需要版本和 provenance。

### 5.7 `context.manager`

职责:

- context budget planning
- retrieval pack selection
- prompt/message assembly
- compression
- summarization
- context partition
- context ledger

Context 分区:

- system/developer instructions
- agent role and objective
- current task
- workflow state
- recent conversation
- retrieved memory
- selected artifacts
- tool results
- constraints and assumptions
- output schema

Context Manager 的输出不是字符串, 而是结构化 `ModelContext`:

- messages
- tool schemas
- attachments
- artifact refs
- memory refs
- omitted candidates
- token budget ledger
- rationale for inclusion

Context Ledger 必须记录:

- 哪些 memory 被选中。
- 哪些 artifact 被引用。
- 哪些候选因为预算被丢弃。
- 每个分区占用多少 token。
- 本次 context 与最终输出的关联。

### 5.8 `state.persistence`

职责:

- transactional state
- event log
- checkpoint
- run snapshot
- artifact store
- vector index
- FTS index

建议存储分层:

```text
SQL Store
  threads / turns / tasks / workflow_runs / agent_sessions
  tool_calls / model_calls / checkpoints / approvals

Event Store
  runtime_events / audit_events / cost_events / context_events

Blob or Artifact Store
  files / diffs / screenshots / long transcripts / logs

Vector Store
  semantic memory / episodic summaries / artifact embeddings

FTS Store
  transcripts / logs / reports / code snippets
```

事件只保存可回放事实和引用, 不保存无限增长的大 payload。

### 5.9 `policy.security`

职责:

- permission policy
- capability grant
- approval
- sandbox
- secret brokerage
- egress policy
- audit
- data masking

`CapabilityGrant` 应绑定:

- agent id
- task id
- run id
- tool/capability id
- workspace scope
- filesystem scope
- network scope
- secret scope
- expiration
- approval requirement
- max cost

安全默认值:

- 默认无 shell exec。
- 默认无全文件系统访问。
- 默认网络访问受限。
- 默认 secret 不进入 prompt。
- 写操作和执行操作默认需要 policy 检查。
- 高风险工具必须可审计和可回放。

### 5.10 `observability.governance`

职责:

- trace
- event viewer
- cost ledger
- context ledger
- audit log
- performance metrics
- failure analytics
- run replay

至少需要追踪:

- run timeline
- node duration
- model latency
- tool latency
- token usage
- cost
- retry count
- cancellation reason
- context composition
- permission decisions
- child agent lineage

### 5.11 `evaluation.replay`

职责:

- golden trace
- workflow replay
- model output snapshot
- tool mock
- memory recall evaluation
- context packing regression
- cost regression
- safety policy tests

评测类型:

- Unit eval: 单个 node/tool/context strategy。
- Workflow eval: 完整工作流输入输出。
- Agent eval: 多轮任务完成质量。
- Memory eval: 是否检索正确记忆, 是否遗漏关键事实。
- Recovery eval: crash 后是否能恢复。
- Governance eval: 权限、审批、脱敏是否生效。

### 5.12 `extension.sdk`

职责:

- manifest schema
- plugin loading
- contribution registry
- permission declaration
- version compatibility
- extension sandbox
- UI contribution

扩展点:

- model provider
- tool provider
- MCP server
- agent preset
- workflow template
- memory backend
- checkpointer
- context strategy
- policy rule
- observability sink
- UI panel
- eval suite

插件 manifest 应声明:

- contributes
- permissions
- runtime requirements
- compatible kernel version
- config schema
- exported capabilities
- side effect level

---

## 6. 核心实体模型

### 6.1 顶层实体

```text
Workspace
  Project
    Thread
      Turn
    Objective
      Task
        TaskDependency
        TaskClaim
    Run
      WorkflowRun
      AgentSession
      NodeStep
      ToolCall
      ModelCall
      Checkpoint
      RuntimeEvent
      Artifact
      MemoryRef
```

### 6.2 对话与任务分离

对话线程负责消息流:

- user message
- assistant message
- tool result message
- system notification
- human approval message

任务流负责目标推进:

- objective
- task
- dependency
- owner
- status
- claim
- progress
- result
- artifact

二者有关联, 但不是同一个东西。一个 thread 可以启动多个 task, 一个 task 可以跨多个 thread 继续推进。

### 6.3 RunState 是视图, 不是唯一事实源

`RunState` 用于调试、恢复和 UI 展示。它不应保存所有事件全文, 而是聚合:

- current status
- current node
- state variables
- active tasks
- latest checkpoint
- recent message refs
- tool call refs
- model call refs
- artifact refs
- memory refs

事实源是 event log、transactional state 和 artifact store。

---

## 7. 标准执行生命周期

### 7.1 用户发起任务

```text
User Input
  -> Thread Turn
  -> Objective/Task created
  -> Workflow selected or generated
  -> Run created
  -> Initial checkpoint
```

### 7.2 Workflow step 执行

```text
Acquire lease
  -> Load checkpoint/state
  -> Build NodeContext
  -> Policy check
  -> Execute node
  -> Normalize result
  -> Append events
  -> Persist artifacts
  -> Reduce state
  -> Save checkpoint
  -> Schedule next command
  -> Release lease
```

### 7.3 Agent step 执行

```text
Load agent session
  -> Retrieve memory candidates
  -> Build context
  -> Call model gateway
  -> Parse structured output/tool call
  -> Execute capability or finish
  -> Persist turn and artifacts
  -> Update working memory
  -> Maybe schedule long-term memory update
```

### 7.4 失败恢复

失败处理顺序:

1. 判断是否 transient。
2. 如果 transient, 按 retry policy 重试。
3. 如果 tool/model call 有 idempotency key, 避免重复副作用。
4. 如果失败可补偿, 执行 compensation step。
5. 如果需要人类输入, 进入 interrupt。
6. 如果不可恢复, 标记 failed 并保留 trace。

---

## 8. 工作流设计

### 8.1 节点类型

内建节点建议:

- `llm`
- `tool`
- `agent`
- `subworkflow`
- `router`
- `condition`
- `parallel`
- `join`
- `memory_read`
- `memory_write`
- `artifact`
- `human_approval`
- `code_execution`
- `evaluation`
- `transform`
- `script`
- `wait_event`
- `compensation`

### 8.2 ExecutionCommand

节点执行结果可返回:

- `continue`
- `goto`
- `branch`
- `spawn_task`
- `spawn_agent`
- `await_task`
- `send_message`
- `emit_event`
- `interrupt`
- `request_approval`
- `retry`
- `compensate`
- `finish`
- `fail`

### 8.3 子工作流

子工作流不应只是嵌套 YAML。它必须有:

- input schema
- output schema
- state mapping
- artifact mapping
- error mapping
- checkpoint namespace
- version

### 8.4 补偿与 Saga

有副作用的 workflow 需要补偿语义。例如:

- 创建文件后失败, 可删除或回滚。
- 修改配置后失败, 可恢复备份。
- 调用外部 API 后失败, 可发起 cancel/refund/revert。

不是所有操作都能补偿, 但必须声明。

---

## 9. 多代理协作设计

### 9.1 Supervisor-Worker

适合复杂任务拆解、并行研究、代码实现和验收。

关键机制:

- supervisor owns objective
- worker owns task
- task result must be structured
- supervisor performs validation
- failed worker task can be reassigned

### 9.2 Group Chat

适合方案讨论、评审、辩论、需求澄清。

关键机制:

- moderator/router
- speaker selection
- turn limit
- consensus rule
- decision artifact

### 9.3 Pipeline

适合固定流程。

关键机制:

- stage input/output schema
- stage artifact
- gate
- rollback/compensation
- versioned workflow template

### 9.4 Blackboard / TaskBoard

适合长期持续推进。

关键机制:

- shared task pool
- task claim
- lease and heartbeat
- priority
- dependency
- blocked reason
- review queue
- done artifact

### 9.5 Handoff

适合把任务从一个 agent 转交给另一个 agent。

关键机制:

- handoff reason
- state summary
- relevant artifacts
- constraints
- expected output
- acceptance criteria

---

## 10. Tool、AgentConnector 与外部能力

### 10.1 Tool Runtime

Tool 是单次可调用能力, 即使它内部是复杂系统, 对上层也应表现为一次调用或流式调用。

Tool 需要声明:

- input schema
- output schema
- side effect level
- required grant
- timeout
- retry policy
- idempotency support
- artifact output support

### 10.2 Workbench

Workbench 是有共享状态的一组工具。例如:

- browser workbench
- database workbench
- git workbench
- code execution workbench
- MCP workbench

Workbench 需要支持:

- list capabilities
- call capability
- save/load state
- close/cleanup
- permission scoping

### 10.3 AgentConnector

AgentConnector 用于接入外部 agent。

它需要支持:

- discover agent capability
- create task/session
- send message
- stream event
- get status
- get artifacts
- cancel
- resume if supported

连接类型:

- local in-process agent
- local CLI agent
- remote HTTP agent
- A2A-like agent
- hosted SDK agent

### 10.4 WorkflowAdapter

Workflow 可以作为 capability 被调用, 但调用时需要:

- workflow id
- version
- input mapping
- output mapping
- state isolation
- checkpoint namespace

---

## 11. Memory 与 Context 详细设计

### 11.1 Retrieval Pack

`RetrievalPack` 是 Memory 与 Context 的桥梁。

它包含:

- working memory snapshot
- relevant episodic summaries
- semantic facts
- procedural recipes
- artifact refs
- confidence score
- source refs
- sensitivity marks
- token estimate

Memory 只返回候选包, 不决定最终 prompt。

### 11.2 Context Budget

Context Manager 应按分区分配预算:

- instructions
- task
- workflow state
- recent messages
- retrieved memory
- artifacts
- tool results
- output schema

预算策略可插拔:

- recency-first
- relevance-first
- safety-first
- cost-aware
- long-task mode
- code-task mode
- research-task mode

### 11.3 压缩策略

压缩优先级:

1. 冗余工具日志。
2. 低相关历史消息。
3. episodic details。
4. 长 artifact 摘要。
5. semantic candidates。
6. 关键约束和验收标准最后压缩。

### 11.4 后台记忆维护

后台任务:

- turn summarization
- task completion summary
- artifact indexing
- memory consolidation
- stale memory pruning
- conflict detection
- skill extraction

这些任务必须也是 workflow, 可被追踪和回放。

---

## 12. 安全与治理

### 12.1 权限分层

权限从低到高:

- read-only context
- read workspace
- write workspace
- run safe command
- run arbitrary command
- network read
- network write
- secret access
- external payment/API mutation

### 12.2 审批策略

审批可以由:

- static policy
- task policy
- user confirmation
- organization policy
- risk classifier

触发审批的常见条件:

- 写文件。
- 执行命令。
- 访问 secret。
- 访问外网。
- 删除或覆盖 artifact。
- 调用高成本模型。
- 发送外部请求。

### 12.3 Secret 处理

原则:

- secret 不进入模型上下文。
- 工具执行时通过 secret broker 注入。
- trace 中脱敏。
- artifact 中不保存明文 secret。
- agent 只能拿到与任务绑定的短期 secret grant。

---

## 13. 观测、回放与评测

### 13.1 Trace Timeline

每个 run 应可视化:

- user input
- workflow step
- agent decision
- model call
- tool call
- memory retrieval
- context build
- permission decision
- artifact output
- child task
- human approval
- final result

### 13.2 Replay

Replay 模式:

- exact replay: 使用记录的模型输出和工具结果。
- partial replay: mock 工具, 重新调用模型。
- model replay: 保留工具结果, 换模型测试。
- recovery replay: 从 checkpoint 恢复。

### 13.3 Evals

推荐建立 eval suite:

- workflow correctness
- tool reliability
- context packing
- memory recall
- subagent delegation
- safety policy
- recovery
- cost regression

---

## 14. 产品宿主设计

Kernel 不绑定 UI, 但需要提供宿主能力。

### 14.1 CLI Host

用于:

- run workflow
- inspect run
- replay
- manage plugins
- manage memory
- run eval

### 14.2 Web/Desktop Host

用于:

- conversation
- taskboard
- workflow trace
- context ledger
- approval
- artifact browser
- agent/team view
- plugin settings

### 14.3 HTTP API

用于:

- create thread/task/run
- stream events
- inspect state
- send human decision
- register external capability
- fetch artifacts

---

## 15. MVP 范围

### 15.1 MVP 必须实现

第一版应实现:

1. Kernel Runtime: run、event、checkpoint、resume、cancel。
2. Workflow Graph: node、edge、condition、subworkflow、interrupt。
3. Model Gateway: 至少两个 provider, streaming, structured output。
4. Tool Runtime: local tool、CLI tool、MCP tool、result envelope。
5. Agent Runtime: oneshot child agent、persistent session、await/cancel。
6. Memory System: working、episodic、artifact。
7. Context Manager: retrieval pack、token budget、context ledger。
8. State Persistence: SQLite/Postgres adapter、event log、artifact store。
9. Policy: basic grants、approval、audit。
10. Observability: trace timeline、cost ledger、replay basic。
11. CLI Host: create/run/inspect/replay。

### 15.2 MVP 不做

第一版不做:

- 完整 marketplace。
- 复杂 visual workflow editor。
- 完整 semantic/procedural memory 自动演化。
- 企业级多租户。
- 去中心化 agent swarm。
- 自动 planner 优化。
- 大规模分布式 worker。

### 15.3 MVP 验证场景

至少验证三个场景:

1. **代码任务**  
   主 agent 拆任务, 子 agent 修改或分析, 工具执行测试, supervisor 验收, 失败可恢复。

2. **研究任务**  
   多 source 检索, artifact 生成, semantic/episodic memory 写入, context ledger 可解释。

3. **长期任务**  
   taskboard 拆分, 暂停, 人工审批, 恢复, 子任务取消, artifact 追踪。

---

## 16. 分阶段演进路线

### Phase 0: 架构固化

产出:

- 核心概念词典。
- 模块边界。
- 事件类型。
- 状态模型。
- 最小 schema。
- 插件 manifest 草案。

### Phase 1: Durable Runtime

建设:

- kernel runtime
- event store
- checkpoint
- workflow graph
- tool runtime
- model gateway

目标:

- 能稳定跑一个可恢复的 graph workflow。

### Phase 2: Agent Orchestration

建设:

- agent session
- child agent
- mailbox
- await/cancel
- task lineage
- basic taskboard

目标:

- 能稳定跑 supervisor-worker 和异步 delegation。

### Phase 3: Memory and Context

建设:

- working memory
- episodic memory
- artifact memory
- retrieval pack
- context manager
- context ledger

目标:

- 长任务中上下文可控、记忆可解释、artifact 可复用。

### Phase 4: Governance and Replay

建设:

- capability grant
- approval
- audit
- replay
- eval suite
- cost ledger

目标:

- 系统可测试、可审计、可回放。

### Phase 5: Ecosystem

建设:

- extension SDK
- plugin registry
- memory backend plugins
- model provider plugins
- workflow templates
- UI panels

目标:

- 内核稳定, 外围能力可扩展。

### Phase 6: Advanced Intelligence

建设:

- semantic/procedural memory 自动沉淀。
- planner。
- reflection/evaluation loop。
- multi-agent group chat。
- advanced taskboard。
- remote/distributed workers。

目标:

- 在稳定 harness 上叠加更强智能。

---

## 17. 关键风险与约束

### 风险 1: 过度抽象

如果一开始把所有能力都抽象成一个统一大接口, 会导致接口臃肿。解决方式是只统一调度协议, 不统一所有内部协议。

### 风险 2: Event Store 膨胀

如果事件保存全部模型输出和工具日志, 存储会迅速失控。解决方式是事件保存引用, 大内容进入 artifact store。

### 风险 3: Memory 污染

如果所有历史自动写入长期记忆, 检索质量会下降。解决方式是写入策略、置信度、来源、TTL、冲突检测。

### 风险 4: Agent 权限过宽

如果 agent 默认继承用户所有权限, 工具系统会非常危险。解决方式是 capability grant、审批、沙箱、secret broker。

### 风险 5: 工作流不可恢复

如果节点副作用没有 idempotency key 和 compensation, retry 会制造重复副作用。解决方式是对副作用工具强制声明幂等和补偿策略。

### 风险 6: 上下文不可解释

如果不知道模型为什么看到某些内容, 就无法调试 agent。解决方式是 context ledger 和 retrieval provenance。

---

## 18. 最终推荐架构愿景

v3 的最终架构愿景是:

> 构建一个以 durable workflow graph 为执行骨架, 以 node-compatible capabilities 为组合边界, 以 model gateway、capability runtime、agent runtime、memory system、context manager 为核心智能运行层, 以 event store、state store、checkpoint、artifact store 为可恢复底座, 以 policy/security、observability、evaluation/replay 为治理基础, 以 manifest extension SDK 保证长期生态扩展的 Agent Kernel。

一句话概括:

> **Graph 负责组合, Runtime 负责可靠, Agent 负责决策, Capability 负责行动, Memory 负责长期知识, Context 负责本轮输入, Policy 负责边界, Event/Trace 负责可解释与可恢复。**

这个架构的关键不是功能数量, 而是边界清楚:

- Agent 不直接管存储, 通过 runtime 和 memory facade。
- Tool 不直接管权限, 通过 capability grant。
- Model 不直接管上下文, 通过 context manager。
- Workflow 不直接管进程, 通过 kernel scheduler。
- Memory 不直接拼 prompt, 通过 retrieval pack。
- Plugin 不直接改内核, 通过 manifest 和 registry。

只要这些边界稳定, 后续扩展高级 planner、复杂团队协作、远程 agent、可视化编排器、企业治理和自动记忆演化时, 不需要推翻核心架构。
