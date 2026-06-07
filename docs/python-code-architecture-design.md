# Agent Kernel Python 代码架构设计文档

更新时间: 2026-06-05

依据: `analysis-report-v2.md` 与 `analysis-report-v3.md`

目标: 以 Python 作为主语言, 将 v2/v3 的 Agent Kernel 架构收敛为可开发、可测试、可演进的代码设计。本文重点描述模块分层、核心领域对象、接口边界、执行流程、状态流转与 MVP 实现路线, 为后续编码提供稳定地基。

---

## 1. 设计结论

最终架构采用 **Durable Workflow Graph + Agent Runtime + Capability Runtime + Memory/Context 分离 + Event/Checkpoint 持久化** 的内核模型。

一句话边界:

> Graph 负责组合, Runtime 负责可靠, Agent 负责决策, Capability 负责行动, Memory 负责长期知识, Context 负责本轮输入, Policy 负责权限边界, Event/Trace 负责可解释和可恢复。

核心选择:

- Python 作为内核主语言。
- 内核优先做稳定 harness, 而不是先做复杂智能。
- 所有可执行能力都是 `Node-compatible executable`, 但不强迫所有对象继承同一个厚接口。
- Memory 与 Context 严格分离, 通过 `RetrievalPack` 对接。
- Context Engineering 是一等架构问题, 目标是最大化上下文信息密度, 而不是填满模型窗口。
- 事件日志采用混合事件溯源: 事件保存事实与引用, 大 payload 进入 Artifact Store。
- 权限通过短期 `CapabilityGrant` 控制, 不给 Agent 永久宽权限。

### 1.1 本轮对照 v2/v3 后的补充重点

当前文档主干与 v3 一致, 但为了支撑后续编码, 需要把以下协议提前固化:

- Conversation 与 Task 必须分离, 不能把聊天线程、任务状态、workflow 状态揉成一个对象。
- 多代理协作不能只描述 `spawn/await/cancel`, 还需要 Supervisor-Worker、Group Chat、Pipeline、TaskBoard、Handoff 的最小协议。
- 子工作流必须声明 input/output/state/artifact/error mapping, 不能只是嵌套配置。
- 有副作用的节点必须声明幂等、补偿与 Saga 语义。
- Tool、Workbench、AgentConnector、WorkflowAdapter 的接口必须分开, 避免把外部 Agent 降级为一次性 Tool。
- Model Gateway 必须维护模型能力声明, 否则无法做 provider fallback、工具调用路由、结构化输出和成本治理。
- Host/API 边界必须明确, Kernel 不绑定 UI, 但要提供 CLI/HTTP/事件流等宿主协议。

### 1.2 Context Information Density 原则

参考 GenericAgent chapter7 的观点, 长期 Agent 的核心挑战不是上下文窗口不够大, 而是有效上下文会被无关工具定义、历史轨迹、工具返回、记忆候选和中间观察稀释。我们的架构需要把“信息密度”作为横切约束。

设计目标:

- 完备性: 当前决策所需的关键信息必须显式进入上下文。
- 简洁性: 无关、重复、过期、低价值信息不能进入上下文。
- 可解释性: 每个进入上下文的片段都要能在 context ledger 中解释来源和理由。
- 可回归: context packing 的长度、组成和命中率必须能被 eval/replay 测试。

四个落点:

- Tool Context: 不把全量工具 schema 每轮塞给模型, 而是按任务、权限、阶段选择可见工具。
- Memory Context: memory 默认不进入 prompt, 只通过 retrieval pack 按需候选进入。
- Runtime Context: 工具长输出、网页原文、日志、trace 默认进入 artifact, 上下文只放摘要和引用。
- Evolution Context: 成功经验应沉淀为 SOP、workflow template、skill card, 下次直接注入精炼程序知识, 而不是重放历史探索。

---

## 2. Python 技术基线

### 2.1 语言与运行时

- Python: `>=3.11`
- 类型系统: `typing`, `Protocol`, `Generic`, `Literal`, `TypedDict`
- 数据模型: `pydantic` v2
- 异步执行: `asyncio`
- CLI: `typer`
- 配置: `pydantic-settings`
- 数据库: `sqlalchemy` 2.x async 或 `sqlmodel`
- 迁移: `alembic`
- 测试: `pytest`, `pytest-asyncio`
- 覆盖率: `coverage.py`
- 日志: `structlog` 或标准库 `logging` + JSON formatter

### 2.2 默认存储

MVP 使用 SQLite 起步, 但接口按可替换后端设计。

- SQL Store: SQLite/Postgres
- Event Store: SQL 表或 append-only 文件后端
- Artifact Store: 本地文件系统
- Vector Store: MVP 可先使用 in-memory/SQLite FTS, 后续替换为 pgvector/Qdrant
- FTS Store: SQLite FTS5

### 2.3 包管理建议

建议使用 `uv` 或 `poetry`。如果项目已有包管理规范, 以后续仓库实际约束为准。

---

## 3. 总体分层

```text
agent_kernel/
  hosts/                  # CLI / HTTP / 后续 Web/Desktop 宿主入口
  app/                    # 应用服务层: 用例编排, 不放领域规则
  domain/                 # 核心领域对象与状态机
  runtime/                # durable runtime, scheduler, lease, checkpoint
  workflow/               # graph, node, edge, reducer, execution command
  agents/                 # agent spec, session, mailbox, child agent orchestration
  capabilities/           # tool runtime, workbench, MCP/CLI/HTTP/SDK adapter, agent connector
  models/                 # model gateway, provider adapter, stream/tool-call normalization
  memory/                 # working/episodic/semantic/procedural/artifact memory
  context/                # retrieval pack selection, budget, model context, ledger
  autonomy/               # exploration, strategy search, workflow induction, skill evolution
  policy/                 # capability grant, approval, sandbox, secret broker
  persistence/            # repositories, event store, state store, artifact store
  observability/          # trace, audit, cost, metrics
  evaluation/             # replay, golden trace, mock model/tool, eval suite
  extensions/             # manifest, plugin registry, contribution loading
  schemas/                # JSON Schema / OpenAPI / public DTO
  shared/                 # ids, clock, errors, result envelope, utils
```

依赖方向:

```text
hosts -> app -> runtime/workflow/agents/capabilities/models/memory/context/policy
runtime -> domain + persistence + observability + policy
workflow -> domain + shared
agents -> domain + models + context + memory + capabilities
capabilities -> domain + policy + persistence
autonomy -> domain + runtime + workflow + agents + capabilities + memory + evaluation
memory -> domain + persistence
context -> domain + memory + persistence
persistence -> domain + shared
domain -> shared
```

约束:

- `domain` 不依赖基础设施实现。
- `workflow` 不直接调用模型、工具或数据库, 只通过 executor/context/repository 接口。
- `agent.runtime` 不直接拼 prompt, 必须调用 `context.manager`。
- `model.gateway` 不执行工具, 只规范化模型调用与工具调用请求。
- `tool.runtime` 不自行判断权限, 必须经过 `policy.security`。
- `autonomy` 只组合已有能力, 不绕过 runtime、workflow、capability、policy 和 memory 边界。
- 插件不得直接修改内核状态, 必须通过 registry 和 runtime 协议接入。

### 3.1 Conversation & Task Hub

v2/v3 都强调对话流和任务流要分离。建议在 `app/` 增加 `conversation_task_hub.py`, 作为 Host 与 Kernel Runtime 之间的应用服务层。

职责:

- 将用户输入写入 `Thread/Turn`。
- 创建或更新 `Objective/Task`。
- 选择 workflow 或创建默认 agent workflow。
- 维护 thread 与 task/run 的关联关系。
- 接收 progress、interrupt、approval、resume 等用户侧操作。
- 不直接执行节点、不直接调用模型、不直接写 memory。

对象边界:

- `Thread` 负责消息时间线。
- `Turn` 负责一次用户/助手/工具/系统消息。
- `Objective` 负责用户目标。
- `Task` 负责可执行工作项、依赖、认领、阻塞和验收。
- `Run` 负责一次 workflow/agent 执行。

关系规则:

- 一个 `Thread` 可以启动多个 `Task`。
- 一个 `Task` 可以跨多个 `Thread` 继续推进。
- 一个 `Task` 可以触发多个 `Run`, 例如 retry、review、repair。
- `RunState` 只引用 thread/task/message/artifact, 不复制完整内容。

建议文件:

```text
app/
  conversation_task_hub.py
  run_service.py
  approval_service.py
  artifact_service.py
  memory_service.py
```

---

## 4. 模块设计

### 4.1 `domain`

职责:

- 定义核心领域对象。
- 定义状态枚举与状态迁移规则。
- 定义值对象、引用对象、事件对象。
- 不包含数据库、HTTP、CLI、模型 SDK 细节。

建议文件:

```text
domain/
  ids.py
  events.py
  states.py
  workspace.py
  conversation.py
  task.py
  run.py
  workflow.py
  agent.py
  capability.py
  model.py
  memory.py
  context.py
  artifact.py
  policy.py
```

关键对象:

- `Workspace`, `Project`
- `Thread`, `Turn`, `Message`
- `Objective`, `Task`, `TaskDependency`, `TaskClaim`
- `Run`, `RunState`, `WorkflowRun`, `NodeStep`
- `AgentSpec`, `AgentSession`, `MailboxMessage`
- `ToolCall`, `ModelCall`
- `Artifact`, `ArtifactRef`
- `MemoryItem`, `MemoryRef`, `RetrievalPack`
- `RuntimeEvent`, `AuditEvent`, `CostEvent`, `ContextEvent`
- `CapabilityRef`, `CapabilityGrant`
- `CheckpointRef`

### 4.2 `runtime`

职责:

- Run 生命周期管理。
- Step 调度。
- lease / heartbeat。
- checkpoint / resume。
- retry / timeout / cancel。
- interrupt / human resume。
- event append。
- dispatch node execution。

建议文件:

```text
runtime/
  engine.py
  scheduler.py
  lease.py
  checkpoint.py
  queue.py
  outbox.py
  dead_letter.py
  budget.py
  circuit_breaker.py
  health.py
  retry.py
  cancellation.py
  dispatcher.py
  recovery.py
```

核心类:

- `KernelRuntime`
- `RunScheduler`
- `LeaseManager`
- `CheckpointManager`
- `NodeDispatcher`
- `RecoveryService`
- `CancellationService`
- `RuntimeQueue`
- `Outbox`
- `DeadLetterQueue`
- `BudgetManager`
- `CircuitBreaker`
- `HealthMonitor`

关键规则:

- 每个 `Run` 必须有 `run_id`。
- 每个 `NodeStep` 必须有 `step_id`, `attempt`, `idempotency_key`。
- 执行 step 前必须 acquire lease。
- 持久化顺序必须是: append event -> persist artifacts -> reduce state -> checkpoint -> schedule next。
- cancel 必须 cascade 到 workflow、child task、agent session、tool call。

稳定性与可恢复性规则:

- Runtime 默认采用 at-least-once 调度, 通过 `idempotency_key` 和状态机去重。
- 外部副作用必须先写 intent event, 再执行, 执行结果必须写 completion event。
- 事件写入和后续调度必须通过 outbox pattern, 避免事件已写但任务未调度。
- 无法恢复或超过重试上限的 step 进入 `DeadLetterQueue`, 不直接丢弃。
- 每个 worker 必须 heartbeat; lease 过期后 step 可被其他 worker 接管。
- 每个 run/task/agent pool 必须可配置时间、成本、tool call、spawn agent、token 预算。
- Provider、CLI、MCP、HTTP adapter 必须有 circuit breaker, 防止故障级联。
- 恢复时优先读取最近 checkpoint, 再回放 checkpoint 后的 runtime events。
- 恢复过程必须幂等, 重复执行 recovery 不应产生重复副作用。
- 健康检查应覆盖 event store、checkpoint store、artifact store、model provider、tool adapter 和 CLI session。

故障模型:

- `worker_crash`: worker 进程崩溃, 由 lease timeout 接管。
- `process_hang`: CLI/tool 无输出或无 heartbeat, 触发 timeout/cancel/kill。
- `provider_outage`: 模型或外部 API 故障, 触发 fallback/circuit breaker。
- `storage_failure`: event/checkpoint/artifact 写入失败, 当前 step 不应确认完成。
- `partial_side_effect`: 外部副作用完成但 completion event 写入失败, 通过 idempotency/reconciliation 修复。
- `context_corruption`: 上下文组装错误或污染, 触发 replay/context regression。
- `memory_conflict`: 记忆冲突或过期, 标记 conflict, 不静默覆盖。

### 4.3 `workflow`

职责:

- 定义 workflow spec。
- 定义 node/edge/schema。
- 执行路由、条件、循环、并行、子工作流。
- 通过 reducer 合并 state patch。

建议文件:

```text
workflow/
  spec.py
  graph.py
  node.py
  edge.py
  command.py
  reducer.py
  executors/
    base.py
    llm.py
    tool.py
    agent.py
    subworkflow.py
    human.py
    memory.py
    transform.py
```

核心对象:

- `WorkflowSpec`
- `NodeSpec`
- `EdgeSpec`
- `NodeExecutor`
- `GraphState`
- `StatePatch`
- `Reducer`
- `ExecutionCommand`
- `SubWorkflowRef`

执行命令:

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

内建节点类型:

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

节点约束:

- 节点输入输出必须有 schema。
- 节点不能直接写全局状态, 只能返回 `StatePatch`。
- 并发 patch 必须通过 reducer 合并。
- 节点可以返回 command, 但不能直接操纵 scheduler。

Skill 与 Workflow 边界:

- `Workflow` 是 Runtime 可直接执行和恢复的显式控制图。
- `Skill` 是 Agent 可读取和解释的程序性知识, 不由 Runtime 直接执行。
- `AgentNode` 可以加载 Skill, 让 Skill 指导当前节点内的判断、工具选择和下一步计划。
- Skill 不能直接改写 workflow graph, 只能经由 Agent 产生 `ExecutionCommand` 或 `PlanPatch`。
- Runtime 收到动态命令后必须校验 schema、权限、版本、checkpoint namespace 和风险预算。
- 如果 Skill 指导出的步骤稳定、可验证、可复用, 才能通过 workflow induction 转化为 `WorkflowTemplate` / `WorkflowSpec`。

子工作流约束:

- 必须声明 `input_schema` 和 `output_schema`。
- 必须声明父 workflow state 到子 workflow state 的 `state_mapping`。
- 必须声明 artifact 如何从子 workflow 返回父 workflow。
- 必须声明错误映射, 例如子 workflow 失败后父节点是 `retry`、`compensate`、`interrupt` 还是 `fail`。
- 必须有独立 checkpoint namespace, 避免父子状态覆盖。
- 必须版本化, 不能只用可变名称引用。

补偿与 Saga:

- 有副作用节点必须声明 `idempotency_key_strategy`。
- 可补偿节点必须声明 `compensation_node_id` 或 `compensation_workflow_ref`。
- 不可补偿节点必须明确标记 `compensable=False`, 由 policy 决定是否需要审批。
- workflow retry 不允许重复执行已成功且不可幂等的副作用 step。
- compensation 失败必须产生独立事件, 不覆盖原始失败原因。

### 4.4 `agents`

职责:

- agent 定义。
- agent session 生命周期。
- child agent spawn / await / cancel。
- mailbox。
- handoff。
- taskboard/team orchestration。
- many-to-many interaction fabric。
- auxiliary observer workflow。
- workspace isolation。
- lineage 追踪。

建议文件:

```text
agents/
  spec.py
  session.py
  runtime.py
  loop.py
  mailbox.py
  interaction_fabric.py
  group_chat.py
  observer.py
  steering.py
  taskboard.py
  handoff.py
  team.py
  lineage.py
```

Agent 类型:

- `oneshot`: 接收任务, 产出结构化结果, 生命周期短。
- `persistent`: 保留长期会话和上下文。
- `team_member`: 团队或任务板中的长期成员。
- `remote`: 通过 `AgentConnector` 接入。
- `human_proxy`: 人类作为 agent-like participant。

Agent Loop 应作为内建 workflow:

```text
load_session
  -> retrieve_memory
  -> build_context
  -> call_model
  -> route_tool_or_finish
  -> execute_capability
  -> persist_turn
  -> update_working_memory
  -> continue_or_stop
```

Agent Planning 规则:

- Agent 可以从 `procedural memory` 读取 Skill。
- Agent 可以基于 Skill 生成本轮行动计划。
- 行动计划必须落成 `ExecutionCommand`、`PlanPatch` 或普通 tool call。
- Agent 不能直接持久化新 workflow, 必须通过 `autonomy.workflow_induction` 或 `workflow_library` 发布。
- Agent 动态添加子工作流时, 只能引用已注册 workflow/template, 或生成 draft template 后请求验证。

协作模式:

- Supervisor-Worker: supervisor 拥有 objective, worker 拥有 task, worker 输出必须结构化, supervisor 负责验收和重派。
- Group Chat: 用于方案讨论和评审, 必须有 moderator/router、speaker selection、turn limit、consensus rule 和 decision artifact。
- Pipeline: 用于固定流程, 每个 stage 必须有 input/output schema、gate、artifact 和 rollback/compensation 策略。
- Blackboard / TaskBoard: 用于长期任务, 依赖 task pool、claim、lease、heartbeat、priority、blocked reason、review queue。
- Handoff: 用于转交任务, 必须携带 reason、state summary、artifact refs、constraints、expected output 和 acceptance criteria。

实现约束:

- `spawn_agent` 只创建或恢复 agent session, 不代表任务完成。
- `await_task` 等待的是结构化 task/agent 事件, 不是 stdout 文本。
- `handoff` 必须写入 lineage, 否则后续 replay 无法解释责任转移。
- TaskBoard 中 worker claim 必须使用 lease, 不能只写 owner 字段。
- Group Chat 的最终结果必须沉淀为 decision artifact, 不能只保留聊天记录。

Group Chat 设计:

- `GroupChatSession` 表示一次围绕议题或事件的讨论室。
- `GroupChatParticipant` 可以是 agent、human、remote agent 或 observer。
- 每个 agent participant 可以使用不同 `model_ref`、不同 role 和不同 instructions。
- `moderator_agent_id` 可选, 如果存在, 主持人负责控场、选择发言者、总结分歧和判断终止。
- `SpeakerPolicy` 决定发言方式: `round_robin`、`fixed_order`、`moderator_select`、`free_for_all`、`event_driven`。
- 用户可以作为 `human_proxy` 参与发言, 也可以作为 observer 只看不说。
- 讨论必须有 `turn_limit`、`time_limit` 或 `consensus_rule`, 防止无限循环。
- 讨论结论必须写入 `decision artifact`, 例如方案结论、争议点、行动项、投票结果。

Group Chat 流程:

```text
create GroupChatSession
  -> attach topic/objective/event
  -> attach participants and optional moderator
  -> choose speaker policy
  -> start discussion workflow
  -> select next speaker
  -> build speaker-specific context
  -> call participant model or wait human input
  -> append group message event
  -> moderator routes/summarizes if configured
  -> check stop condition
  -> produce decision artifact
```

`free_for_all` 不是让模型无限自发执行。实现上仍由 scheduler 驱动, 每轮向候选 participant 询问 `want_to_speak` 或由 moderator 选择发言者。

Interaction Fabric 设计:

- 多 Agent 协作不能只建模为一个 group chat。复杂项目中, 后端、前端、测试、架构、人类、辅助观察者之间会形成多对多持续交互网络。
- `InteractionChannel` 表示一个有主题、有参与者、有权限边界的持续沟通通道。
- channel 可以绑定 `Task`、`Artifact`、`WorkflowRun`、`AgentSession` 或 `Objective`。
- 一个 agent 可以同时在多个 channel 中交互, 例如 backend API channel、test feedback channel、architecture review channel。
- 人类也是 participant, 可以发言、旁观、审批、纠偏或提出事实更新。
- 交互消息必须是事件和 artifact 的一部分, 不能只存在 provider/CLI 原始上下文里。
- channel 可以是 `round_robin`、`ad_hoc`、`request_response`、`broadcast`、`review_queue` 或 `observer_feedback`。

多实例 Agent Pool:

- 同一角色可以有多个实例, 例如 10 个 Codex backend worker。
- `AgentPool` 管理相同或相近能力的 agent session 集合。
- TaskBoard 分配任务时可以按 capability、current load、context ownership、workspace isolation、historical success rate 选择 agent。
- 多实例之间不能默认共享完整上下文, 应通过 channel、artifact 和 context pack 精准传递。
- 如果多个 agent 修改同一 workspace, 必须使用 worktree/sandbox/merge workflow 或显式锁。

Auxiliary Observer Workflow:

- 辅助工作流是一类旁路观察者, 用于观察主 agent 的执行轨迹、上下文、工具调用和产物。
- 它不接管主流程, 默认只提出 `HumanIntervention`、`PlanPatch`、risk warning 或 review comment。
- 适合做事实检查、幻觉检测、遗忘提醒、约束一致性检查、危险命令提醒、测试反馈整理。
- 辅助观察者可以由 agent、workflow 或 human 扮演。
- 如果观察者需要阻断主流程, 必须通过 `RuntimeControlRequest` 或 policy hook。

### 4.5 `capabilities`

职责:

- tool registry。
- tool runtime。
- workbench。
- MCP client/server bridge。
- CLI / HTTP / SDK adapter。
- workflow adapter。
- agent connector。
- result normalization。

建议文件:

```text
capabilities/
  registry.py
  runtime.py
  tool.py
  result.py
  workbench.py
  adapters/
    local.py
    cli.py
    http.py
    sdk.py
    mcp.py
    workflow.py
    agent.py
```

核心对象:

- `CapabilityRef`
- `CapabilitySpec`
- `ToolSpec`
- `ToolRuntime`
- `ToolCallRequest`
- `ToolResult`
- `Workbench`
- `AgentConnector`
- `WorkflowAdapter`

`Tool` 是单次可调用能力。`AgentConnector` 不是普通 Tool, 它必须保留多轮 session、task status、artifact、progress event、cancellation、streaming 和 mailbox 语义。

Tool 必须声明:

- input schema
- output schema
- side effect level
- required grant
- timeout
- retry policy
- idempotency support
- artifact output support

Workbench 是一组共享状态工具的宿主, 例如 browser、database、git、code execution、MCP workbench。

Workbench 必须支持:

- `list_capabilities`
- `call_capability`
- `save_state`
- `load_state`
- `close`
- permission scoping

AgentConnector 必须支持:

- `discover`
- `create_session`
- `send_message`
- `stream_events`
- `get_status`
- `get_artifacts`
- `cancel`
- `resume`

WorkflowAdapter 必须声明:

- workflow id
- version
- input mapping
- output mapping
- state isolation
- checkpoint namespace

### 4.6 `models`

职责:

- provider abstraction。
- streaming normalization。
- structured output enforcement。
- tool-call normalization。
- model capability registry。
- rate limit / fallback。
- cost accounting。
- message format conversion。

建议文件:

```text
models/
  gateway.py
  provider.py
  request.py
  response.py
  stream.py
  tool_call.py
  capabilities.py
  cost.py
  providers/
    openai.py
    anthropic.py
    local.py
```

`ModelGateway` 只负责模型调用治理, 不负责长期记忆、工具执行或 workflow routing。

模型能力声明必须包含:

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
- rate limit profile

模型调用规则:

- 上游传入 `ModelContext`, 不传入自由拼接 prompt。
- provider 返回必须规范化为统一 `ModelResult`。
- tool call 只作为模型请求意图返回, 由 Capability Runtime 执行。
- structured output 校验失败时, 由 Model Gateway 按策略 retry、repair 或返回 `model_failure`。
- 成本、token、latency 必须写入 cost ledger 和 trace。

### 4.7 `memory`

职责:

- memory write policy。
- memory retrieval。
- memory compaction。
- retention / forgetting。
- conflict resolution。
- provenance。
- indexing。

建议文件:

```text
memory/
  facade.py
  working.py
  episodic.py
  semantic.py
  procedural.py
  artifact.py
  retrieval.py
  policy.py
  consolidation.py
```

五层记忆:

- Working Memory: 当前目标、约束、待办、已验证事实。
- Episodic Memory: 历史任务、会话摘要、关键事件、工具轨迹。
- Semantic Memory: 稳定事实、用户偏好、项目知识、团队约定。
- Procedural Memory: SOP、skills、workflow templates、repair recipes。
- Artifact Memory: 文件、diff、报告、图片、测试结果、中间数据。

写入原则:

- 不允许所有 turn 自动写入长期记忆。
- 长期记忆写入必须经过 policy 或后台 summarizer。
- 高敏感内容默认不进入 semantic memory。
- 冲突记忆不能静默覆盖, 必须保留版本和 provenance。

### 4.8 `context`

职责:

- retrieval pack selection。
- context budget planning。
- message assembly。
- compression / summarization。
- context partition。
- context ledger。
- information density scoring。
- tool schema visibility planning。
- stale context pruning。
- context quality gate。

建议文件:

```text
context/
  manager.py
  budget.py
  packer.py
  compression.py
  ledger.py
  partitions.py
  token_counter.py
  density.py
  tool_visibility.py
  quality_gate.py
```

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

输出对象是结构化 `ModelContext`, 不是拼好的单个字符串。

Context Engineering 规则:

- 每次模型调用前必须先构造 `ContextPlan`, 再装配 `ModelContext`。
- `ContextPlan` 需要声明每个分区预算、候选来源、压缩策略、排除原因。
- 工具 schema 按当前 node/agent/task/policy 选择, 不默认注入所有工具。
- 多轮任务中, 原始历史逐步降级为 summary、artifact ref 或 episodic memory ref。
- 上下文中出现的约束、验收标准、风险提示优先级高于普通历史消息。
- 检索出的 memory 只是候选, 必须经过 relevance、freshness、sensitivity、token cost 过滤。
- 长工具结果默认转 artifact, 上下文只保留摘要、关键字段和 artifact ref。
- 每次 context build 必须输出 density score 和 omitted candidates, 供调试和 eval。

Context Budget 策略:

- `recency_first`
- `relevance_first`
- `safety_first`
- `cost_aware`
- `long_task_mode`
- `code_task_mode`
- `research_task_mode`

压缩优先级:

1. 冗余工具日志。
2. 低相关历史消息。
3. episodic details。
4. 长 artifact 摘要。
5. semantic candidates。
6. 关键约束和验收标准最后压缩。

质量门禁:

- 如果缺少当前任务、关键约束或 output schema, context build 应失败或请求补齐。
- 如果 tool schema 总 token 超过预算, 必须做工具可见性裁剪。
- 如果 retrieved memory 冲突, 必须保留冲突标记, 不能静默选择其中一个。
- 如果上下文主要由低相关历史填充, 应触发 compaction。
- 如果模型连续失败, 应记录 context regression signal, 供 replay/eval 分析。

后台记忆维护:

- turn summarization。
- task completion summary。
- artifact indexing。
- memory consolidation。
- stale memory pruning。
- conflict detection。
- skill extraction。

这些后台任务也必须作为 workflow 执行, 以便 trace、retry 和 replay。

### 4.9 `autonomy`

职责:

- open-ended task exploration。
- candidate strategy generation。
- tool/workflow composition search。
- reflection and self-critique。
- success criteria verification。
- trace distillation。
- workflow induction。
- procedural memory write。
- reusable workflow publication。

建议文件:

```text
autonomy/
  planner.py
  explorer.py
  strategy.py
  verifier.py
  reflector.py
  trace_distiller.py
  workflow_induction.py
  workflow_library.py
  skill_evolution.py
```

核心原则:

- 自主探索仍然必须运行在 `runtime` 和 `workflow` 上, 不能绕过 checkpoint、event、policy 和 artifact。
- 工具层保持稳定和原子化, 优先进化 SOP、script、workflow template 和 procedural memory。
- 每次探索尝试必须形成 `ExplorationAttempt`, 记录假设、工具组合、结果、失败原因和 artifact。
- 只有经过 verifier 验证的成功 trace 才能进入 workflow induction。
- 沉淀结果必须是结构化 `WorkflowSpec` 或 `ProcedureMemory`, 不能只保存自然语言总结。
- 新生成 workflow 默认进入 draft 状态, 需要 eval/replay 或人工确认后才能作为 stable 子工作流被其他流程调用。

自主探索流程:

```text
Open task
  -> infer objective and acceptance criteria
  -> generate candidate strategies
  -> select tools/workflows/agents
  -> execute attempt under runtime
  -> observe result and artifacts
  -> reflect and adjust strategy
  -> verify success criteria
  -> distill successful trace
  -> induce reusable WorkflowSpec
  -> write procedural memory
  -> publish workflow template
```

策略搜索范围:

- tool sequence: 多个工具按顺序尝试。
- workflow composition: 组合已有 workflow/subworkflow。
- agent delegation: 委派给子 agent 或群聊评审。
- parameter search: 调整工具参数、模型、上下文策略。
- recovery recipe: 为失败场景生成修复流程。

沉淀对象:

- `WorkflowSpec`: 可执行 workflow。
- `ProcedureMemory`: 适用条件、前置条件、步骤解释、常见失败。
- `GoldenTrace`: 成功执行轨迹。
- `EvalSuite`: 回归测试。
- `Artifact`: 示例输入输出、报告、diff、日志。
- `SkillCard`: 给人和 agent 阅读的能力说明。

发布门槛:

- 至少一次完整成功执行。
- 有明确 input/output schema。
- 有 acceptance criteria。
- 有失败处理或限制说明。
- 有成本与风险评估。
- 有 replay 或 eval 记录。

### 4.10 `persistence`

职责:

- repository 接口与实现。
- transactional state。
- event log。
- checkpoint。
- artifact store。
- vector/FTS index。

建议文件:

```text
persistence/
  repositories.py
  unit_of_work.py
  event_store.py
  state_store.py
  checkpoint_store.py
  artifact_store.py
  vector_store.py
  fts_store.py
  sqlite/
    models.py
    repositories.py
    migrations/
```

存储分层:

```text
SQL Store
  threads / turns / tasks / workflow_runs / agent_sessions
  tool_calls / model_calls / checkpoints / approvals

Event Store
  runtime_events / audit_events / cost_events / context_events

Artifact Store
  files / diffs / screenshots / long transcripts / logs

Vector Store
  semantic memory / episodic summaries / artifact embeddings

FTS Store
  transcripts / logs / reports / code snippets
```

### 4.11 `policy`

职责:

- permission policy。
- capability grant。
- approval。
- sandbox。
- secret broker。
- egress policy。
- audit。
- data masking。

建议文件:

```text
policy/
  grant.py
  engine.py
  approval.py
  sandbox.py
  secrets.py
  egress.py
  audit.py
  masking.py
```

默认安全策略:

- 默认无 shell exec。
- 默认无全文件系统访问。
- 默认网络访问受限。
- 默认 secret 不进入 prompt。
- 写操作和执行操作默认需要 policy check。
- 高风险工具必须可审计、可回放。

权限分层:

- read-only context
- read workspace
- write workspace
- run safe command
- run arbitrary command
- network read
- network write
- secret access
- external payment/API mutation

审批触发条件:

- 写文件。
- 执行命令。
- 访问 secret。
- 访问外网。
- 删除或覆盖 artifact。
- 调用高成本模型。
- 发送外部 mutation 请求。

Secret 处理:

- secret 不进入模型上下文。
- 工具执行时通过 secret broker 注入。
- trace 中必须脱敏。
- artifact 中不保存明文 secret。
- agent 只能拿到与任务绑定的短期 secret grant。

### 4.12 `observability`

职责:

- trace timeline。
- audit log。
- cost ledger。
- context ledger。
- metrics。
- failure analytics。

建议文件:

```text
observability/
  trace.py
  audit.py
  cost.py
  metrics.py
  timeline.py
```

### 4.13 `evaluation`

职责:

- golden trace。
- workflow replay。
- tool mock。
- model output snapshot。
- memory recall eval。
- context packing regression。
- cost regression。
- policy tests。

建议文件:

```text
evaluation/
  replay.py
  golden_trace.py
  mocks.py
  suites.py
  assertions.py
```

### 4.14 `extensions`

职责:

- manifest schema。
- plugin loading。
- contribution registry。
- permission declaration。
- compatibility check。
- extension sandbox。

建议文件:

```text
extensions/
  manifest.py
  loader.py
  registry.py
  permissions.py
  compatibility.py
```

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
- eval suite

### 4.15 `hosts`

Kernel 不绑定 UI, 但需要提供稳定宿主协议。

CLI Host 用于:

- run workflow
- inspect run
- replay
- manage plugins
- manage memory
- run eval

Web/Desktop Host 用于:

- conversation
- taskboard
- workflow trace
- context ledger
- approval
- artifact browser
- agent/team view
- plugin settings

HTTP API 用于:

- create thread/task/run
- stream events
- inspect state
- send human decision
- register external capability
- fetch artifacts

建议文件:

```text
hosts/
  cli.py
  http.py
  event_stream.py
  dto.py
```

Host 约束:

- Host 只能调用 `app/` 服务, 不直接调用 `NodeExecutor`。
- HTTP streaming 只暴露规范化事件, 不暴露 provider 原始事件。
- 用户审批、取消、恢复都必须通过应用服务写入事件。
- UI 状态从 `RunState`、`Task`、`TraceTimeline`、`ContextLedger` 读取, 不反向修改内核状态。

---

## 5. 核心领域对象草案

以下是用于指导编码的 Python 伪代码。实际实现时应放入 `domain/` 并补齐校验、序列化和测试。

### 5.1 标识和值对象

```python
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, Field


class EntityRef(BaseModel):
    id: str
    kind: str


class ArtifactRef(BaseModel):
    artifact_id: str
    uri: str
    media_type: str | None = None
    version: str | None = None
    checksum: str | None = None


class MemoryRef(BaseModel):
    memory_id: str
    memory_type: Literal["working", "episodic", "semantic", "procedural", "artifact"]
    score: float | None = None
```

### 5.2 事件模型

```python
class RuntimeEvent(BaseModel):
    event_id: str
    event_type: str
    run_id: str
    timestamp: datetime
    node_id: str | None = None
    step_id: str | None = None
    agent_id: str | None = None
    task_id: str | None = None
    causal_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
```

事件类型建议:

- `run.created`
- `run.started`
- `run.paused`
- `run.resumed`
- `run.completed`
- `run.failed`
- `run.cancelled`
- `step.started`
- `step.completed`
- `step.failed`
- `model.call.started`
- `model.call.completed`
- `tool.call.started`
- `tool.call.completed`
- `memory.read`
- `memory.write`
- `context.built`
- `approval.requested`
- `approval.resolved`
- `artifact.created`

### 5.3 Conversation 与 Task 对象

```python
MessageRole = Literal["user", "assistant", "tool", "system", "human_approval"]
TaskStatus = Literal["created", "ready", "claimed", "running", "blocked", "review", "done", "failed", "cancelled"]


class Message(BaseModel):
    message_id: str
    role: MessageRole
    content: dict[str, Any]
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    created_at: datetime


class Thread(BaseModel):
    thread_id: str
    workspace_id: str
    title: str | None = None
    message_refs: list[EntityRef] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class Objective(BaseModel):
    objective_id: str
    thread_id: str | None = None
    description: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    created_at: datetime


class Task(BaseModel):
    task_id: str
    objective_id: str
    title: str
    status: TaskStatus
    owner_agent_id: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    blocked_reason: str | None = None
    result_artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class TaskClaim(BaseModel):
    claim_id: str
    task_id: str
    claimant_id: str
    lease_id: str
    heartbeat_at: datetime
    expires_at: datetime
```

### 5.4 Group Chat 与 Runtime Steering 对象

```python
SpeakerPolicyType = Literal["round_robin", "fixed_order", "moderator_select", "free_for_all", "event_driven"]
ParticipantKind = Literal["agent", "human", "remote_agent", "observer"]
InterventionType = Literal[
    "correction",
    "constraint_update",
    "environment_update",
    "priority_change",
    "stop_instruction",
    "approval_hint",
]
InterventionApplyMode = Literal["continue_next_turn", "pause_and_resume", "cancel_current_step_and_resume"]


class SpeakerPolicy(BaseModel):
    type: SpeakerPolicyType
    fixed_order: list[str] = Field(default_factory=list)
    max_turns_per_participant: int | None = None
    allow_user_interrupt: bool = True
    require_moderator_approval: bool = False


class GroupChatParticipant(BaseModel):
    participant_id: str
    kind: ParticipantKind
    agent_session_id: str | None = None
    human_user_id: str | None = None
    role: str
    can_speak: bool = True
    can_moderate: bool = False


class GroupChatSession(BaseModel):
    group_chat_id: str
    thread_id: str
    objective_id: str | None = None
    topic: str
    participant_ids: list[str]
    moderator_participant_id: str | None = None
    speaker_policy: SpeakerPolicy
    turn_limit: int
    consensus_rule: str | None = None
    decision_artifact_ref: ArtifactRef | None = None
    status: Literal["created", "running", "paused", "completed", "cancelled"]
    created_at: datetime
    updated_at: datetime


class DiscussionTurn(BaseModel):
    turn_id: str
    group_chat_id: str
    speaker_participant_id: str
    message_id: str
    turn_index: int
    selected_by: str | None = None
    rationale: str | None = None
    created_at: datetime


ChannelMode = Literal["round_robin", "ad_hoc", "request_response", "broadcast", "review_queue", "observer_feedback"]
ObserverAction = Literal["comment", "warn", "intervene", "propose_patch", "request_pause", "request_cancel"]


class InteractionParticipant(BaseModel):
    participant_id: str
    kind: ParticipantKind
    agent_session_id: str | None = None
    human_user_id: str | None = None
    role: str
    permissions: list[str] = Field(default_factory=list)


class InteractionChannel(BaseModel):
    channel_id: str
    topic: str
    mode: ChannelMode
    participant_ids: list[str]
    bound_objective_id: str | None = None
    bound_task_id: str | None = None
    bound_run_id: str | None = None
    bound_artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    retention_policy: str | None = None
    created_at: datetime
    updated_at: datetime


class InteractionMessage(BaseModel):
    message_id: str
    channel_id: str
    sender_participant_id: str
    content: dict[str, Any]
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    causal_event_id: str | None = None
    created_at: datetime


class AgentPool(BaseModel):
    pool_id: str
    name: str
    role: str
    agent_session_ids: list[str] = Field(default_factory=list)
    capability_tags: list[str] = Field(default_factory=list)
    selection_policy: Literal["least_loaded", "capability_match", "round_robin", "manual", "success_rate"] = "capability_match"


class ObservationPolicy(BaseModel):
    observe_targets: list[str] = Field(default_factory=list)
    watch_event_types: list[str] = Field(default_factory=list)
    check_facts: bool = True
    check_constraints: bool = True
    check_tool_risk: bool = True
    check_context_drift: bool = True
    max_interruptions: int = 3


class AuxiliaryObserverWorkflow(BaseModel):
    observer_id: str
    name: str
    target_run_id: str
    observer_agent_id: str | None = None
    observer_workflow_ref: str | None = None
    policy: ObservationPolicy
    channel_id: str | None = None
    status: Literal["created", "observing", "paused", "completed", "cancelled"]
    created_at: datetime
    updated_at: datetime


class ObservationFinding(BaseModel):
    finding_id: str
    observer_id: str
    target_run_id: str
    severity: Literal["info", "warning", "critical"]
    action: ObserverAction
    message: str
    evidence_event_refs: list[str] = Field(default_factory=list)
    evidence_artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    proposed_intervention: HumanIntervention | None = None
    proposed_plan_patch: "PlanPatch | None" = None
    created_at: datetime


class HumanIntervention(BaseModel):
    intervention_id: str
    run_id: str
    thread_id: str | None = None
    task_id: str | None = None
    type: InterventionType
    content: str
    priority: Literal["normal", "high", "critical"] = "normal"
    apply_mode: InterventionApplyMode
    created_at: datetime


class RuntimeControlRequest(BaseModel):
    control_id: str
    run_id: str
    target_type: Literal["run", "node_step", "tool_call", "agent_session", "group_chat"]
    target_id: str
    action: Literal["pause", "resume", "cancel", "kill", "inject_intervention"]
    reason: str
    intervention: HumanIntervention | None = None
    created_at: datetime
```

### 5.5 Stability 与 Recovery 对象

```python
RecoveryStatus = Literal["pending", "running", "succeeded", "failed", "dead_lettered"]
CircuitStatus = Literal["closed", "open", "half_open"]
BudgetScope = Literal["run", "task", "agent", "agent_pool", "workspace"]


class RecoveryJob(BaseModel):
    recovery_id: str
    target_type: Literal["run", "node_step", "tool_call", "agent_session"]
    target_id: str
    status: RecoveryStatus
    reason: str
    last_checkpoint_id: str | None = None
    replay_from_event_id: str | None = None
    attempt: int = 0
    created_at: datetime
    updated_at: datetime


class DeadLetterItem(BaseModel):
    item_id: str
    target_type: Literal["run", "node_step", "tool_call", "model_call", "agent_session"]
    target_id: str
    reason: str
    failure_type: str
    event_refs: list[str] = Field(default_factory=list)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    retryable: bool = False
    created_at: datetime


class RuntimeBudget(BaseModel):
    budget_id: str
    scope: BudgetScope
    scope_id: str
    max_wall_time_seconds: int | None = None
    max_cost_usd: float | None = None
    max_model_calls: int | None = None
    max_tool_calls: int | None = None
    max_spawned_agents: int | None = None
    max_tokens: int | None = None
    exhausted_action: Literal["pause", "cancel", "request_approval"] = "pause"


class CircuitBreakerState(BaseModel):
    circuit_id: str
    target_ref: str
    status: CircuitStatus
    failure_count: int = 0
    opened_at: datetime | None = None
    next_probe_at: datetime | None = None
```

### 5.6 RunState

```python
RunStatus = Literal[
    "pending",
    "running",
    "paused",
    "interrupted",
    "failed",
    "completed",
    "cancelled",
]


class RunState(BaseModel):
    run_id: str
    status: RunStatus
    thread_id: str | None = None
    workflow_id: str | None = None
    workflow_version: str | None = None
    agent_id: str | None = None
    task_id: str | None = None
    current_node_id: str | None = None
    variables: dict[str, Any] = Field(default_factory=dict)
    active_task_ids: list[str] = Field(default_factory=list)
    message_refs: list[EntityRef] = Field(default_factory=list)
    tool_call_refs: list[EntityRef] = Field(default_factory=list)
    model_call_refs: list[EntityRef] = Field(default_factory=list)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    memory_refs: list[MemoryRef] = Field(default_factory=list)
    checkpoint_id: str | None = None
    started_at: datetime | None = None
    updated_at: datetime
```

`RunState` 是聚合视图, 不是唯一事实源。事实源是 event log、transactional state、checkpoint 和 artifact store。

### 5.7 Workflow 对象

```python
class NodeSpec(BaseModel):
    node_id: str
    kind: str
    capability_ref: str | None = None
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    input_mapping: dict[str, Any] = Field(default_factory=dict)
    output_mapping: dict[str, Any] = Field(default_factory=dict)
    retry_policy: dict[str, Any] | None = None
    timeout_seconds: int | None = None


class EdgeSpec(BaseModel):
    from_node: str
    to_node: str
    condition: str | None = None


class WorkflowSpec(BaseModel):
    workflow_id: str
    version: str
    name: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    nodes: list[NodeSpec]
    edges: list[EdgeSpec]
    start_node_id: str
```

### 5.8 ExecutionCommand

```python
class ExecutionCommand(BaseModel):
    type: Literal[
        "continue",
        "goto",
        "branch",
        "spawn_task",
        "spawn_agent",
        "await_task",
        "send_message",
        "emit_event",
        "interrupt",
        "request_approval",
        "retry",
        "compensate",
        "finish",
        "fail",
    ]
    target: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
```

### 5.9 NodeExecutor 协议

```python
from typing import Protocol


class NodeContext(BaseModel):
    run_id: str
    step_id: str
    node: NodeSpec
    state: dict[str, Any]
    input: dict[str, Any]
    grants: list[str] = Field(default_factory=list)
    idempotency_key: str


class NodeResult(BaseModel):
    state_patch: dict[str, Any] = Field(default_factory=dict)
    command: ExecutionCommand | None = None
    events: list[RuntimeEvent] = Field(default_factory=list)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)


class NodeExecutor(Protocol):
    async def execute(self, ctx: NodeContext) -> NodeResult:
        ...
```

### 5.10 Agent 对象

```python
AgentMode = Literal["oneshot", "persistent", "team_member", "remote", "human_proxy"]
AgentStatus = Literal["defined", "starting", "running", "waiting_input", "idle", "completed", "failed", "cancelled"]


class AgentSpec(BaseModel):
    agent_id: str
    name: str
    mode: AgentMode
    model_ref: str
    role: str | None = None
    instructions: str | None = None
    toolset: list[str] = Field(default_factory=list)
    memory_profile: str | None = None
    isolation: Literal["shared", "worktree", "sandbox", "remote"] = "sandbox"
    permission_policy_id: str | None = None


class AgentSession(BaseModel):
    session_id: str
    agent_id: str
    status: AgentStatus
    parent_session_id: str | None = None
    task_id: str | None = None
    mailbox_id: str | None = None
    created_at: datetime
    updated_at: datetime
```

### 5.11 Capability 对象

```python
SideEffectLevel = Literal["none", "read", "write", "network", "exec", "external_mutation"]


class CapabilitySpec(BaseModel):
    capability_id: str
    name: str
    kind: Literal["tool", "workbench", "workflow", "agent_connector", "human"]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    side_effect_level: SideEffectLevel
    timeout_seconds: int | None = None
    supports_streaming: bool = False
    supports_idempotency: bool = False
    required_grant: str | None = None


class CapabilityGrant(BaseModel):
    grant_id: str
    capability_id: str
    agent_id: str | None = None
    task_id: str | None = None
    run_id: str | None = None
    workspace_scope: str | None = None
    filesystem_scope: list[str] = Field(default_factory=list)
    network_scope: list[str] = Field(default_factory=list)
    secret_scope: list[str] = Field(default_factory=list)
    expires_at: datetime
    approval_required: bool = False
    max_cost_usd: float | None = None
```

### 5.12 Tool、Workbench 与 AgentConnector 协议

```python
class ToolResult(BaseModel):
    ok: bool
    output: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    events: list[RuntimeEvent] = Field(default_factory=list)


class WorkbenchProtocol(Protocol):
    async def list_capabilities(self) -> list[CapabilitySpec]:
        ...

    async def call_capability(self, capability_id: str, input: dict[str, Any], ctx: dict[str, Any]) -> ToolResult:
        ...

    async def save_state(self) -> dict[str, Any]:
        ...

    async def load_state(self, state: dict[str, Any]) -> None:
        ...

    async def close(self) -> None:
        ...


class AgentConnectorProtocol(Protocol):
    async def discover(self) -> CapabilitySpec:
        ...

    async def create_session(self, input: dict[str, Any]) -> AgentSession:
        ...

    async def send_message(self, session_id: str, message: Message) -> None:
        ...

    async def stream_events(self, session_id: str):
        ...

    async def get_status(self, session_id: str) -> AgentStatus:
        ...

    async def get_artifacts(self, session_id: str) -> list[ArtifactRef]:
        ...

    async def cancel(self, session_id: str, reason: str) -> None:
        ...
```

### 5.13 Model Gateway 对象

```python
class ModelCapability(BaseModel):
    model_ref: str
    provider: str
    context_window: int
    supports_tools: bool = False
    supports_parallel_tool_calls: bool = False
    supports_structured_output: bool = False
    supports_multimodal_input: bool = False
    supports_image_output: bool = False
    supports_audio_output: bool = False
    supports_reasoning_traces: bool = False
    supports_streaming: bool = False
    input_cost_per_1m_tokens: float | None = None
    output_cost_per_1m_tokens: float | None = None
    latency_profile: dict[str, Any] = Field(default_factory=dict)
    rate_limit_profile: dict[str, Any] = Field(default_factory=dict)


class ModelCallRequest(BaseModel):
    model_ref: str
    context: "ModelContext"
    response_schema: dict[str, Any] | None = None
    stream: bool = False
    idempotency_key: str


class ModelResult(BaseModel):
    output: dict[str, Any] | None = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    raw_artifact_ref: ArtifactRef | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    finish_reason: str | None = None
```

### 5.14 Memory 与 Context 对象

```python
class MemoryItem(BaseModel):
    memory_id: str
    memory_type: Literal["working", "episodic", "semantic", "procedural", "artifact"]
    scope: str
    content: dict[str, Any]
    source_event_ids: list[str] = Field(default_factory=list)
    source_artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    confidence: float | None = None
    importance: float | None = None
    sensitivity: Literal["public", "internal", "confidential", "secret"] = "internal"
    ttl_seconds: int | None = None
    version: str = "1"
    created_by: str | None = None
    verified_by: str | None = None
    last_used_at: datetime | None = None


class RetrievalPack(BaseModel):
    working_snapshot: dict[str, Any] = Field(default_factory=dict)
    episodic_refs: list[MemoryRef] = Field(default_factory=list)
    semantic_refs: list[MemoryRef] = Field(default_factory=list)
    procedural_refs: list[MemoryRef] = Field(default_factory=list)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    confidence_score: float | None = None
    sensitivity_marks: list[str] = Field(default_factory=list)
    token_estimate: int | None = None


class ModelContext(BaseModel):
    messages: list[dict[str, Any]]
    tool_schemas: list[dict[str, Any]] = Field(default_factory=list)
    attachments: list[ArtifactRef] = Field(default_factory=list)
    memory_refs: list[MemoryRef] = Field(default_factory=list)
    omitted_candidates: list[MemoryRef] = Field(default_factory=list)
    token_budget_ledger: dict[str, int] = Field(default_factory=dict)
    inclusion_rationale: dict[str, str] = Field(default_factory=dict)


class ContextCandidate(BaseModel):
    candidate_id: str
    source_type: Literal["message", "memory", "artifact", "tool_result", "workflow_state", "schema", "instruction"]
    source_ref: str
    token_estimate: int
    relevance_score: float
    freshness_score: float | None = None
    sensitivity: Literal["public", "internal", "confidential", "secret"] = "internal"
    include: bool = False
    rationale: str | None = None


class ContextPlan(BaseModel):
    plan_id: str
    run_id: str
    model_ref: str
    partition_budgets: dict[str, int]
    candidates: list[ContextCandidate] = Field(default_factory=list)
    selected_candidate_ids: list[str] = Field(default_factory=list)
    omitted_candidate_ids: list[str] = Field(default_factory=list)
    tool_visibility: list[str] = Field(default_factory=list)
    compression_strategy: str | None = None
    density_score: float | None = None
    quality_warnings: list[str] = Field(default_factory=list)
```

### 5.15 Skill 与 Dynamic Plan 对象

```python
SkillStatus = Literal["draft", "active", "deprecated"]
SkillExecutionMode = Literal["agent_interpreted", "compiled_workflow"]
PlanPatchStatus = Literal["proposed", "accepted", "rejected", "applied"]


class SkillUsePolicy(BaseModel):
    allowed_agent_ids: list[str] = Field(default_factory=list)
    allowed_workflow_ids: list[str] = Field(default_factory=list)
    required_grants: list[str] = Field(default_factory=list)
    max_risk_level: Literal["low", "medium", "high"] = "medium"
    require_human_approval: bool = False


class SkillCard(BaseModel):
    skill_id: str
    name: str
    description: str
    status: SkillStatus = "draft"
    execution_mode: SkillExecutionMode = "agent_interpreted"
    when_to_use: str
    instructions: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)
    recommended_tools: list[str] = Field(default_factory=list)
    recommended_workflows: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    failure_modes: list[str] = Field(default_factory=list)
    use_policy: SkillUsePolicy | None = None
    procedure_memory_ref: MemoryRef | None = None
    compiled_workflow_ref: str | None = None
    examples: list[ArtifactRef] = Field(default_factory=list)


class PlanPatch(BaseModel):
    patch_id: str
    run_id: str
    proposed_by_agent_id: str
    reason: str
    commands: list[ExecutionCommand]
    required_capabilities: list[str] = Field(default_factory=list)
    expected_artifacts: list[str] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)
    status: PlanPatchStatus = "proposed"
    created_at: datetime
```

Skill 调用规则:

- `agent_interpreted` skill 只能进入 `ModelContext`, 由 Agent 解释执行。
- `compiled_workflow` skill 可以指向已验证 `WorkflowSpec`, 由 Runtime 作为 subworkflow 调度。
- `PlanPatch` 是 Skill/Agent 动态修改当前执行计划的唯一合法载体。
- Runtime 可以拒绝 `PlanPatch`, 拒绝原因必须写入事件。

### 5.16 Autonomy 与 Workflow Induction 对象

```python
ExplorationStatus = Literal["created", "planning", "running", "verifying", "distilling", "published", "failed", "cancelled"]
AttemptStatus = Literal["planned", "running", "succeeded", "failed", "cancelled"]
WorkflowTemplateStatus = Literal["draft", "verified", "stable", "deprecated"]


class AcceptanceCriteria(BaseModel):
    criteria_id: str
    description: str
    verifier_ref: str | None = None
    required: bool = True


class ExplorationTask(BaseModel):
    exploration_id: str
    objective_id: str
    task_id: str | None = None
    status: ExplorationStatus
    problem_statement: str
    acceptance_criteria: list[AcceptanceCriteria] = Field(default_factory=list)
    max_attempts: int = 5
    risk_budget: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class CandidateStrategy(BaseModel):
    strategy_id: str
    exploration_id: str
    hypothesis: str
    tool_refs: list[str] = Field(default_factory=list)
    workflow_refs: list[str] = Field(default_factory=list)
    agent_refs: list[str] = Field(default_factory=list)
    expected_artifacts: list[str] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)
    priority: int = 0


class ExplorationAttempt(BaseModel):
    attempt_id: str
    exploration_id: str
    strategy_id: str
    run_id: str
    status: AttemptStatus
    result_summary: str | None = None
    failure_reason: str | None = None
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    event_refs: list[str] = Field(default_factory=list)
    started_at: datetime | None = None
    completed_at: datetime | None = None


class GoldenTrace(BaseModel):
    trace_id: str
    source_run_id: str
    source_attempt_id: str
    event_refs: list[str]
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    verified_by: str | None = None
    created_at: datetime


class WorkflowTemplate(BaseModel):
    template_id: str
    name: str
    status: WorkflowTemplateStatus
    workflow_spec_ref: str
    source_trace_id: str | None = None
    applicability: str
    limitations: list[str] = Field(default_factory=list)
    cost_profile: dict[str, Any] = Field(default_factory=dict)
    risk_profile: dict[str, Any] = Field(default_factory=dict)
    eval_suite_ref: str | None = None
    version: str = "0.1.0"


class SkillEvolutionRecord(BaseModel):
    record_id: str
    source_trace_id: str
    skill_id: str | None = None
    workflow_template_id: str | None = None
    decision: Literal["create_skill", "update_skill", "compile_workflow", "reject"]
    rationale: str
    created_at: datetime
```

### 5.17 Extension Manifest 草案

```python
class ExtensionContribution(BaseModel):
    kind: Literal[
        "model_provider",
        "tool_provider",
        "mcp_server",
        "agent_preset",
        "workflow_template",
        "memory_backend",
        "checkpointer",
        "context_strategy",
        "policy_rule",
        "observability_sink",
        "eval_suite",
    ]
    name: str
    entrypoint: str
    config_schema: dict[str, Any] = Field(default_factory=dict)


class ExtensionManifest(BaseModel):
    extension_id: str
    name: str
    version: str
    compatible_kernel: str
    contributes: list[ExtensionContribution] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    runtime_requirements: dict[str, Any] = Field(default_factory=dict)
    side_effect_level: SideEffectLevel = "none"
```

---

## 6. 状态流转

### 6.1 Run 状态机

```text
pending
  -> running
  -> paused
  -> running
  -> interrupted
  -> running
  -> completed

running
  -> failed
  -> cancelled

failed
  -> running      # resume/retry when recoverable

paused
  -> cancelled

interrupted
  -> cancelled
```

状态说明:

- `pending`: run 已创建但未开始调度。
- `running`: scheduler 正在推进 step。
- `paused`: 系统或用户主动暂停。
- `interrupted`: 等待人类输入、审批或外部事件。
- `completed`: 正常完成。
- `failed`: 不可自动恢复或超过 retry policy。
- `cancelled`: 用户或上游级联取消。

### 6.2 NodeStep 状态机

```text
scheduled
  -> leased
  -> running
  -> succeeded

running
  -> retry_wait
  -> failed
  -> cancelled
  -> interrupted

retry_wait
  -> scheduled
```

关键规则:

- `leased` 防止多个 worker 重复执行同一 step。
- `retry_wait` 必须保留 attempt 和 idempotency_key。
- `interrupted` 可恢复, `failed` 不一定可恢复。

### 6.3 Task 状态机

```text
created
  -> ready
  -> claimed
  -> running
  -> review
  -> done

running
  -> blocked
  -> failed
  -> cancelled

blocked
  -> ready

failed
  -> ready        # reassigned/retry
```

关键规则:

- `TaskClaim` 必须有 lease 与 heartbeat。
- `blocked` 必须记录 blocked reason。
- `review` 用于 supervisor 验收 worker 输出。

### 6.4 AgentSession 状态机

```text
defined
  -> starting
  -> running
  -> waiting_input
  -> idle
  -> running
  -> completed

running
  -> failed
  -> cancelled
```

关键规则:

- 不依赖终端文本 marker 判断完成。
- 子 Agent 就绪应使用结构化事件, 例如 `agent.turn.completed`。
- `oneshot` 完成后可回收 session。
- `persistent` 进入 `idle` 后等待下一个 mailbox message。

### 6.5 Approval 状态机

```text
requested
  -> approved
  -> rejected
  -> expired
  -> cancelled
```

审批是正常状态, 不是异常。workflow 进入 `interrupted` 后等待 approval resolution。

### 6.6 ToolCall 实时控制状态机

```text
requested
  -> awaiting_approval
  -> approved
  -> running
  -> succeeded

running
  -> pausing
  -> paused
  -> cancelling
  -> cancelled
  -> killing
  -> killed
  -> failed
```

关键规则:

- 高风险命令在 `awaiting_approval` 阶段必须可拒绝, 不应先执行再回滚。
- 用户发出 `cancel tool_call` 后, CLI adapter 应先向受控 process group 发送 SIGTERM。
- 超过 grace period 后仍未退出, 才允许进入 `killing` 并发送 SIGKILL。
- `paused` 只适用于可暂停能力; 普通 shell 命令通常只能 cancel/kill。
- tool call 结束后必须写入 `tool.call.cancelled`、`tool.call.killed` 或 `tool.call.failed` 事件。

### 6.7 Human Intervention 状态机

```text
created
  -> applied
  -> superseded
  -> rejected
```

应用模式:

- `continue_next_turn`: 不暂停当前安全步骤, 下一次模型调用重建上下文时纳入提醒。
- `pause_and_resume`: run 进入 `interrupted`, 更新 working memory / task constraints 后恢复。
- `cancel_current_step_and_resume`: 取消当前 node/tool/model step, 从最近 checkpoint 或下一个安全点恢复。

原则:

- 不改写历史消息和历史事件, 只追加 `human.intervention` 事件。
- 不重建原 `Thread`、`Task`、`Run`, 除非用户明确要求重新开始。
- 纠偏内容进入 `ContextManager` 的高优先级分区, 并写入 context ledger。
- 如果纠偏改变验收标准或环境事实, 必须更新 working memory 或 task constraints。

### 6.8 RecoveryJob 状态机

```text
pending
  -> running
  -> succeeded

running
  -> failed
  -> dead_lettered

failed
  -> pending      # retry when recoverable
```

关键规则:

- recovery 只重建状态和调度, 不应直接重复执行已确认成功的副作用。
- recovery 前必须检查 event log、checkpoint 和 artifact refs 是否一致。
- recovery 后必须写入 `recovery.succeeded` 或 `recovery.failed` 事件。
- 不可恢复对象进入 dead letter, 等待人工或专门 repair workflow。

### 6.9 CircuitBreaker 状态机

```text
closed
  -> open
  -> half_open
  -> closed

half_open
  -> open
```

关键规则:

- `closed`: 正常调用。
- `open`: 暂停调用故障 provider/tool/connector, 快速失败或 fallback。
- `half_open`: 允许少量探测请求。
- circuit 状态变化必须进入 trace 和 metrics。

---

## 7. 标准执行流程

### 7.1 用户发起任务

```text
User Input
  -> create Thread Turn
  -> create Objective / Task
  -> select or generate WorkflowSpec
  -> create Run
  -> append run.created
  -> save initial checkpoint
  -> schedule start node
```

### 7.2 Workflow step 执行

```text
Acquire lease
  -> Load checkpoint and state
  -> Build NodeContext
  -> Policy check
  -> Execute node
  -> Normalize result
  -> Append runtime events
  -> Persist artifacts
  -> Reduce state patch
  -> Save checkpoint
  -> Schedule next command
  -> Release lease
```

### 7.3 Agent step 执行

```text
Load AgentSession
  -> Retrieve memory candidates
  -> Build RetrievalPack
  -> Build ModelContext
  -> Call ModelGateway
  -> Parse structured output or tool calls
  -> Execute Capability or finish
  -> Persist turn and artifacts
  -> Update working memory
  -> Maybe schedule long-term memory workflow
```

### 7.4 Tool call 执行

```text
Resolve CapabilitySpec
  -> Validate input schema
  -> Request CapabilityGrant
  -> Approval if needed
  -> Execute adapter
  -> Normalize ToolResult
  -> Persist artifacts
  -> Append tool events
  -> Return structured output
```

### 7.5 失败恢复

```text
Failure detected
  -> classify failure
  -> transient? retry with backoff
  -> side effect? use idempotency key
  -> compensable? execute compensation
  -> needs human? interrupt
  -> unrecoverable? mark failed and retain trace
```

失败分类:

- `deterministic_failure`: 输入/schema/业务规则错误。
- `transient_failure`: 网络、限流、临时不可用。
- `policy_failure`: 权限或审批失败。
- `model_failure`: 模型超时、格式不合法、provider 错误。
- `tool_failure`: 工具执行失败。
- `system_failure`: runtime 或存储异常。

### 7.6 系统恢复流程

```text
Runtime starts or worker recovers
  -> Scan running/leased steps
  -> Detect expired leases and incomplete tool/model calls
  -> Load latest checkpoint
  -> Replay events after checkpoint
  -> Reconcile artifacts and side effects
  -> Requeue recoverable steps
  -> Dead-letter unrecoverable items
  -> Emit recovery report artifact
```

恢复策略:

- `worker_crash`: lease 过期后重新调度未完成 step。
- `cli_hang`: timeout 后 cancel/kill, 如果可恢复则重启 session 并注入 state summary。
- `provider_outage`: circuit breaker open, 使用 fallback provider 或暂停等待。
- `storage_failure`: 不确认 step 完成, 等待存储恢复后重试。
- `partial_side_effect`: 用 idempotency key 查询外部状态, 补写 completion event 或启动 compensation。
- `budget_exhausted`: 按 `RuntimeBudget.exhausted_action` pause/cancel/request approval。

### 7.7 Group Chat 讨论流程

```text
Create GroupChatSession
  -> Attach participants
  -> Optional moderator starts
  -> Select next speaker by SpeakerPolicy
  -> Build participant-specific ModelContext
  -> Agent or human produces message
  -> Append group_chat.message event
  -> Moderator summarizes/routes if configured
  -> Check consensus/turn/time limit
  -> Continue or produce decision artifact
```

用户参与规则:

- 用户发言作为普通 `Message` 追加到同一 `Thread`。
- 用户不发言时可以作为 observer, 不影响 speaker policy。
- 用户可以随时通过 `HumanIntervention` 修改议题、纠正假设或要求主持人收敛。

### 7.8 多对多 CLI Agent Mesh 编排流程

```text
Create AgentPool for each role
  -> Start persistent CLI AgentSessions
  -> Create TaskBoard and InteractionChannels
  -> Coordinator assigns tasks to pools or specific agents
  -> Agents exchange messages through channels
  -> Shared decisions become artifacts
  -> Test/review agents publish findings
  -> Coordinator creates repair tasks
  -> Repeat until acceptance criteria pass
```

适用场景:

- 一个后端角色对应多个 Codex/Claude Code CLI session。
- 前端、后端、测试、架构角色之间多对多沟通。
- 某个节点对接真实人类, 由人类提供事实、审批或指导。
- 不同 agent 拥有不同上下文和不同 workspace。

关键约束:

- agent 之间不直接互相写对方上下文, 只能通过 `InteractionChannel`、artifact、task update 交互。
- 每个 channel 都必须有 topic、participants、mode、retention policy。
- 共享事实必须沉淀为 artifact, 例如 API contract、ADR、test report。
- 多个 coding agent 修改代码时必须隔离 workspace, 合并由专门 merge/review workflow 处理。
- Coordinator 负责跨 channel 汇总状态, 不应让每个 agent 全量读取所有消息。

### 7.9 危险命令暂停/取消流程

```text
Model requests tool call
  -> CapabilityRuntime classifies side effect
  -> Policy requires approval for risky command
  -> User approves/rejects
  -> CLIAdapter starts command in managed process group
  -> User sends cancel/pause if command looks wrong
  -> RuntimeControlRequest emitted
  -> CLIAdapter sends SIGTERM
  -> Wait grace period
  -> SIGKILL if needed
  -> Persist cancellation event
  -> Workflow enters interrupted/cancelled/resumable state
```

实现要求:

- Agent 不允许直接裸跑命令, 必须经过 `CapabilityRuntime`。
- 每个 CLI tool call 必须有 `tool_call_id`、`process_group_id`、`idempotency_key`。
- UI/CLI/HTTP 必须能向 run、node_step、tool_call 发送控制请求。
- 长任务节点必须周期性检查 cancellation token。
- 取消已经产生副作用的命令后, runtime 应根据 compensation 声明决定是否启动补偿流程。

### 7.10 Human Intervention 运行时纠偏流程

```text
User notices misunderstanding/environment change
  -> Send HumanIntervention
  -> Append human.intervention event
  -> Update working memory or task constraints
  -> Choose apply mode
  -> Rebuild ModelContext with high-priority correction
  -> Resume current run from safe point
```

适用场景:

- 模型误解需求。
- 用户新增约束。
- 外部环境变化。
- 用户发现当前执行方向错误。
- 用户希望主持人调整讨论节奏或收敛结论。

不变性:

- 原对话不变。
- 原任务不变。
- 原事件不改写。
- 原 run 可继续, 只追加纠偏事件并从 checkpoint 或下一安全点恢复。

### 7.11 Auxiliary Observer Workflow 流程

```text
Main agent/workflow starts
  -> Attach AuxiliaryObserverWorkflow
  -> Observer subscribes to selected events/artifacts/context ledger
  -> Observer detects hallucination/risk/constraint drift
  -> Emit ObservationFinding
  -> Optionally propose HumanIntervention or PlanPatch
  -> Runtime validates action
  -> Main run continues, pauses, or adjusts
```

观察目标:

- 模型是否忽略关键约束。
- 当前操作是否和任务背景冲突。
- 工具调用是否危险。
- 产物是否和 API contract / ADR / test report 冲突。
- 长任务是否出现上下文漂移。
- agent 是否在错误假设上持续推进。

权限边界:

- observer 默认只读。
- observer 提醒不等于强制中断。
- critical finding 可以触发 `request_pause`, 但仍需 Runtime/Policy 判断。
- observer 不能直接修改主 agent 的私有上下文, 只能追加事件、channel message、intervention 或 plan patch。

### 7.12 Autonomous Exploration 与 Workflow Induction 流程

```text
User gives open-ended task
  -> Create ExplorationTask
  -> Infer acceptance criteria
  -> Generate candidate strategies
  -> Rank strategy by risk/cost/expected value
  -> Execute ExplorationAttempt as normal Run
  -> Observe events/artifacts/tool results
  -> Reflect on failure or partial success
  -> Try next strategy or adjust current strategy
  -> Verify success criteria
  -> Distill successful GoldenTrace
  -> Induce WorkflowSpec and ProcedureMemory
  -> Create WorkflowTemplate in draft
  -> Run replay/eval or request human review
  -> Publish as stable reusable subworkflow
```

关键约束:

- 探索不是无限循环, 必须受 `max_attempts`、成本、时间、权限和风险预算约束。
- 每次尝试都必须是普通 `Run`, 享受 checkpoint、cancel、policy、event、artifact、trace。
- 失败尝试也要保留, 因为它们是后续 verifier、risk profile 和 repair recipe 的输入。
- 成功 trace 归纳出的 workflow 必须 schema 化, 并声明适用条件和限制。
- 发布后的 workflow 可以被 `WorkflowAdapter` 当作 capability 调用, 也可以被其他 workflow 作为 `subworkflow` 使用。

反射触发条件:

- 工具调用失败。
- 结果未满足 acceptance criteria。
- 上下文证据不足。
- 成本或重试次数接近预算。
- 用户通过 `HumanIntervention` 指出方向错误。
- verifier 判定输出不可用。

### 7.13 Skill 与 Dynamic Workflow 互操作流程

子工作流使用 Skill:

```text
SubWorkflow enters AgentNode
  -> ContextManager selects relevant SkillCard
  -> Agent reads skill instructions
  -> Agent decides tool/subworkflow actions
  -> Agent emits ExecutionCommand
  -> Runtime validates and schedules
```

Skill 指导添加子工作流:

```text
Agent reads SkillCard
  -> Skill suggests using or creating subworkflow
  -> Agent emits PlanPatch
  -> Runtime validates schema/policy/version/risk
  -> Runtime accepts or rejects PlanPatch
  -> If accepted, schedule subworkflow
  -> Append plan_patch.applied event
```

不允许:

- Skill 直接执行 shell。
- Skill 直接写 `RunState`。
- Skill 直接修改 `WorkflowGraph`。
- Skill 绕过 `Policy` 调 Tool。
- Skill 插入未校验节点。

演进路径:

```text
Skill guides Agent
  -> repeated successful attempts
  -> GoldenTrace
  -> WorkflowTemplate draft
  -> eval/replay/human review
  -> stable WorkflowSpec
  -> callable subworkflow
```

---

## 8. 关键接口边界

### 8.1 Runtime 调 Workflow

```python
class WorkflowEngine:
    async def start(self, workflow: WorkflowSpec, input: dict[str, Any]) -> RunState:
        ...

    async def resume(self, run_id: str, payload: dict[str, Any] | None = None) -> RunState:
        ...

    async def cancel(self, run_id: str, reason: str) -> RunState:
        ...
```

### 8.2 Agent 调 Context 与 Model

```python
class AgentLoop:
    async def run_turn(self, session_id: str, task_input: dict[str, Any]) -> dict[str, Any]:
        retrieval_pack = await self.memory.retrieve(...)
        model_context = await self.context_manager.build_context(...)
        model_result = await self.model_gateway.call(model_context)
        return await self.route_model_result(model_result)
```

### 8.3 Capability Runtime

```python
class CapabilityRuntime:
    async def call(self, capability_id: str, input: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
        spec = await self.registry.get(capability_id)
        await self.policy.require_grant(spec, ctx)
        result = await self.adapter_for(spec).call(input, ctx)
        return self.normalize(result)
```

### 8.4 Memory 与 Context

```python
class MemoryFacade:
    async def retrieve(self, request: dict[str, Any]) -> RetrievalPack:
        ...

    async def write(self, item: MemoryItem) -> MemoryRef:
        ...


class ContextManager:
    async def plan_context(self, request: dict[str, Any]) -> ContextPlan:
        ...

    async def build_context(self, request: dict[str, Any]) -> ModelContext:
        ...

    async def record_ledger(self, context: ModelContext) -> None:
        ...

    async def evaluate_density(self, plan: ContextPlan) -> dict[str, Any]:
        ...
```

### 8.5 Runtime Control

```python
class RuntimeControlService:
    async def pause_run(self, run_id: str, reason: str) -> RunState:
        ...

    async def resume_run(self, run_id: str, payload: dict[str, Any] | None = None) -> RunState:
        ...

    async def cancel_run(self, run_id: str, reason: str) -> RunState:
        ...

    async def cancel_tool_call(self, tool_call_id: str, reason: str) -> ToolResult:
        ...

    async def inject_intervention(self, intervention: HumanIntervention) -> RunState:
        ...
```

### 8.6 Runtime Reliability

```python
class RecoveryService:
    async def create_recovery_job(self, target_type: str, target_id: str, reason: str) -> RecoveryJob:
        ...

    async def recover(self, recovery_id: str) -> RecoveryJob:
        ...

    async def reconcile_side_effects(self, run_id: str) -> dict[str, Any]:
        ...


class BudgetManager:
    async def check_budget(self, scope: str, scope_id: str) -> RuntimeBudget:
        ...

    async def consume(self, scope: str, scope_id: str, usage: dict[str, Any]) -> RuntimeBudget:
        ...


class CircuitBreaker:
    async def allow_request(self, target_ref: str) -> bool:
        ...

    async def record_success(self, target_ref: str) -> CircuitBreakerState:
        ...

    async def record_failure(self, target_ref: str, reason: str) -> CircuitBreakerState:
        ...
```

### 8.7 Group Chat Runtime

```python
class GroupChatRuntime:
    async def create_session(self, input: dict[str, Any]) -> GroupChatSession:
        ...

    async def add_human_message(self, group_chat_id: str, message: Message) -> DiscussionTurn:
        ...

    async def run_next_turn(self, group_chat_id: str) -> DiscussionTurn:
        ...

    async def pause(self, group_chat_id: str, reason: str) -> GroupChatSession:
        ...

    async def finish(self, group_chat_id: str) -> ArtifactRef:
        ...
```

### 8.8 Interaction Fabric Runtime

```python
class InteractionFabricRuntime:
    async def create_channel(self, input: dict[str, Any]) -> InteractionChannel:
        ...

    async def add_participant(self, channel_id: str, participant: InteractionParticipant) -> InteractionChannel:
        ...

    async def send_message(self, channel_id: str, message: InteractionMessage) -> InteractionMessage:
        ...

    async def route_message(self, message_id: str, target_channel_ids: list[str]) -> list[InteractionMessage]:
        ...

    async def summarize_channel(self, channel_id: str) -> ArtifactRef:
        ...
```

### 8.9 Agent Pool Scheduler

```python
class AgentPoolScheduler:
    async def create_pool(self, input: dict[str, Any]) -> AgentPool:
        ...

    async def select_agent(self, pool_id: str, task_id: str, context: dict[str, Any]) -> AgentSession:
        ...

    async def assign_task(self, pool_id: str, task_id: str) -> TaskClaim:
        ...
```

### 8.10 Auxiliary Observer Runtime

```python
class AuxiliaryObserverRuntime:
    async def attach_observer(self, input: dict[str, Any]) -> AuxiliaryObserverWorkflow:
        ...

    async def evaluate_event(self, observer_id: str, event: RuntimeEvent) -> list[ObservationFinding]:
        ...

    async def publish_finding(self, finding: ObservationFinding) -> None:
        ...

    async def request_action(self, finding_id: str) -> RuntimeControlRequest | PlanPatch | HumanIntervention | None:
        ...
```

### 8.11 Autonomous Exploration Runtime

```python
class AutonomyRuntime:
    async def start_exploration(self, input: dict[str, Any]) -> ExplorationTask:
        ...

    async def generate_strategies(self, exploration_id: str) -> list[CandidateStrategy]:
        ...

    async def run_attempt(self, strategy_id: str) -> ExplorationAttempt:
        ...

    async def verify_attempt(self, attempt_id: str) -> dict[str, Any]:
        ...

    async def distill_trace(self, attempt_id: str) -> GoldenTrace:
        ...

    async def induce_workflow(self, trace_id: str) -> WorkflowTemplate:
        ...

    async def publish_workflow(self, template_id: str) -> WorkflowTemplate:
        ...
```

接口约束:

- `run_attempt` 必须创建普通 `Run`, 不允许创建绕过 runtime 的私有执行。
- `verify_attempt` 可以调用 eval suite、tool result checker、human review 或 agent judge, 但结果必须结构化。
- `induce_workflow` 输出必须包含 `WorkflowSpec`、input/output schema、error mapping 和 artifact mapping。
- `publish_workflow` 必须检查 replay/eval/human review 状态, 未验证模板只能以 draft 引用。

### 8.12 Skill Runtime Boundary

```python
class SkillService:
    async def retrieve_skills(self, request: dict[str, Any]) -> list[SkillCard]:
        ...

    async def inject_skill_context(self, skill_ids: list[str], context_plan: ContextPlan) -> ContextPlan:
        ...

    async def propose_plan_patch(self, run_id: str, agent_id: str, skill_id: str, proposal: dict[str, Any]) -> PlanPatch:
        ...


class PlanPatchValidator:
    async def validate(self, patch: PlanPatch) -> dict[str, Any]:
        ...

    async def apply(self, patch: PlanPatch) -> RunState:
        ...
```

边界规则:

- `SkillService` 只负责检索、注入和生成候选计划补丁。
- `PlanPatchValidator` 负责 schema、policy、capability、workflow version、risk budget 校验。
- `apply` 必须通过 `KernelRuntime` 调度, 不能直接修改内存中的 workflow。
- 所有 patch proposal、reject、apply 都必须写入事件。

---

## 9. MVP 开发范围

第一版只实现稳定内核, 不追求完整生态。

必须实现:

1. `kernel.runtime`: run、event、checkpoint、resume、cancel。
2. `workflow.graph`: node、edge、condition、subworkflow、interrupt。
3. `model.gateway`: 至少两个 provider, streaming, structured output。
4. `capability.runtime`: local tool、CLI tool、MCP tool、result envelope。
5. `agent.runtime`: oneshot child agent、persistent session、await/cancel。
6. `memory.system`: working、episodic、artifact。
7. `context.manager`: retrieval pack、token budget、context ledger。
8. `state.persistence`: SQLite adapter、event log、artifact store。
9. `policy.security`: basic grants、approval、audit。
10. `observability`: trace timeline、cost ledger、basic replay。
11. `hosts.cli`: create/run/inspect/replay。

暂不实现:

- 完整 marketplace。
- 复杂 visual workflow editor。
- 完整 semantic/procedural memory 自动演化。
- 企业级多租户。
- 去中心化 agent swarm。
- 自动 planner 优化。
- 完整自主探索和 workflow induction 自动发布。
- 大规模分布式 worker。

### 9.1 能力地图覆盖

对照 v2 的 10 大能力域, MVP 覆盖关系如下:

| 能力域 | MVP 覆盖模块 | 第一版深度 |
|---|---|---|
| 定位与核心抽象 | `domain`, `workflow`, `runtime` | Agent Kernel, 非产品宿主 |
| 推理能力 | `agents.loop`, `models.gateway` | 基础 agent loop, 不做高级 planner |
| 执行能力 | `capabilities`, `policy` | local/CLI/MCP tool, 权限检查 |
| 编排能力 | `workflow`, `runtime` | DAG、条件、interrupt、subworkflow |
| 协作能力 | `agents`, `app` | supervisor-worker、task lineage、基础 taskboard、interaction channel 草案 |
| 记忆能力 | `memory` | working、episodic、artifact |
| 上下文能力 | `context` | retrieval pack、budget、ledger |
| 运行时能力 | `runtime`, `persistence` | event、checkpoint、resume、cancel、retry |
| 安全与治理 | `policy`, `observability` | grant、approval、audit、cost |
| 集成与产品化 | `hosts`, `extensions` | CLI、HTTP 草案、manifest 草案 |
| 自主探索与沉淀 | `autonomy`, `evaluation`, `memory` | MVP 只保留接口草案, 后续阶段实现 |

MVP 明确不追求能力域全量深度, 但每个能力域必须有最小接口, 避免后续扩展时推翻模块边界。

---

## 10. 推荐开发顺序

### Phase 0: 架构固化

产出:

- `domain` 核心模型。
- 状态机测试。
- 事件类型清单。
- 最小数据库 schema。
- 插件 manifest 草案。

验收:

- 所有领域对象可序列化。
- 状态迁移非法路径有测试。
- 事件 payload 不保存大内容, 只保存引用。

### Phase 1: Durable Runtime

建设:

- `runtime.engine`
- `event_store`
- `checkpoint_store`
- `outbox`
- `dead_letter`
- `budget_manager`
- `circuit_breaker`
- `workflow.graph`
- `tool.runtime`
- `model.gateway` mock provider

验收:

- 可运行一个 3 节点 workflow。
- 中途 crash 后可从 checkpoint 恢复。
- retry 不重复执行幂等 tool call。
- provider/tool 故障会触发 circuit breaker。
- 超预算 run 会 pause 或 request approval。
- 不可恢复 step 会进入 dead letter。

### Phase 2: Agent Orchestration

建设:

- `AgentSession`
- `AgentLoop`
- child agent spawn / await / cancel
- mailbox
- task lineage

验收:

- supervisor 可创建 oneshot child agent。
- child agent 完成后通过结构化事件通知 supervisor。
- cancel 可级联到 child agent。

### Phase 3: Memory and Context

建设:

- working memory。
- episodic memory。
- artifact memory。
- retrieval pack。
- context budget。
- context ledger。

验收:

- Agent turn 能解释本次上下文为何包含某些 memory/artifact。
- 长工具日志进入 artifact, 不直接塞入 event payload。

### Phase 4: Governance and Replay

建设:

- capability grant。
- approval。
- audit log。
- replay。
- eval suite。
- cost ledger。

验收:

- 写文件、执行命令、访问网络会触发 policy check。
- exact replay 可不调用真实模型和工具复现 run timeline。

### Phase 5: Extension SDK

建设:

- manifest。
- plugin loader。
- contribution registry。
- provider/tool/context strategy 扩展点。

验收:

- 新工具 provider 可通过 manifest 注册。
- 插件声明的权限会进入 policy check。

### Phase 6: Autonomous Exploration

建设:

- exploration task。
- candidate strategy generation。
- attempt execution。
- reflection。
- verifier。
- trace distillation。
- workflow induction。
- workflow template publication。

验收:

- 给定开放任务, agent 能在预算内尝试多种工具/工作流组合。
- 成功链路能被蒸馏为 `GoldenTrace`。
- `GoldenTrace` 能归纳出 draft `WorkflowTemplate`。
- 通过 eval/replay 或人工确认后, 模板能作为 subworkflow 被其他流程调用。

### Phase 7: Multi-Agent Interaction Fabric

建设:

- interaction channel。
- agent pool scheduler。
- multi-CLI persistent sessions。
- observer workflow。
- cross-channel summary。
- merge/review workflow。

验收:

- 一个角色可由多个 CLI agent 实例共同承担。
- 前端、后端、测试、架构和人类可通过多个 channel 多对多交互。
- observer 可旁路发现约束漂移、事实错误或危险操作。
- observer 的建议能转化为 intervention、plan patch 或 runtime control request。

---

## 11. 测试策略

### 11.1 单元测试

重点覆盖:

- 状态机迁移。
- reducer 合并规则。
- schema validation。
- policy decision。
- context budget allocation。
- context density scoring。
- tool visibility pruning。
- memory retrieval filtering。
- retry classification。
- agent pool selection policy。
- observer finding classification。
- budget exhaustion handling。
- circuit breaker state transition。

### 11.2 集成测试

重点覆盖:

- workflow run 完整执行。
- checkpoint/resume。
- interrupt/approval/resume。
- child agent spawn/await/cancel。
- tool call event/artifact persistence。
- context ledger 写入。
- context compaction after long run。
- multi-agent interaction channel routing。
- auxiliary observer intervention flow。
- crash recovery from checkpoint and event replay。
- dead-letter unrecoverable step。
- CLI hang timeout and kill flow。

### 11.3 回放测试

重点覆盖:

- exact replay。
- partial replay。
- recovery replay。
- model replay。
- context packing regression。
- multi-agent replay with channel messages。
- recovery replay after worker crash。
- cost regression。

### 11.4 测试边界

- public function 都应有测试。
- 外部 provider、HTTP、CLI、MCP 使用 mock/fake。
- 高风险权限路径必须有拒绝测试。
- 目标覆盖率不低于 80%。

---

## 12. 关键工程约束

1. 不要把 Agent、Tool、Workflow、Memory 写成同一个巨型基类。
2. 不要让 ModelGateway 负责 prompt 拼装或工具执行。
3. 不要让 Memory 直接决定最终 prompt。
4. 不要让 Tool 自己绕过 Policy。
5. 不要把大 payload 写入 RuntimeEvent。
6. 不要把 conversation state、task state、workflow state、agent session state 混成一个状态机。
7. 所有副作用操作必须有 idempotency key, 能补偿的必须声明 compensation。
8. 所有长期任务必须支持 checkpoint、resume、cancel。
9. 所有模型调用、工具调用、权限决策、上下文构建都必须可追踪。
10. MVP 优先做可恢复、可审计、可测试, 再做复杂 planner 和高级多代理协作。
11. Skill 不由 Runtime 直接执行; Skill 只能进入 Agent context, 或被编译/归纳为 WorkflowSpec 后由 Runtime 执行。
12. Skill 动态添加流程时必须生成 `PlanPatch`, 由 Runtime 校验和应用。
13. Runtime 是唯一可以调度 Tool、SubWorkflow、Agent 和 Human 节点的组件。
14. Agent/人类/CLI 之间的多对多沟通必须通过 `InteractionChannel` 或 artifact, 不能依赖 provider 私有会话状态作为唯一事实源。
15. Auxiliary Observer 默认只读, 需要暂停、取消或改写计划时必须发出 `RuntimeControlRequest`、`HumanIntervention` 或 `PlanPatch`。
16. 多个 coding agent 修改同一项目时必须有 workspace isolation、merge/review workflow 或显式锁。
17. Runtime 默认按 at-least-once 执行设计, 所有 step/tool/model call 必须通过 idempotency key 去重。
18. 事件写入、artifact 写入、checkpoint 和下一步调度必须有明确事务边界; 不能出现“状态已变但事件缺失”的隐性成功。
19. 所有外部 provider/tool/CLI connector 必须支持 timeout、retry policy、circuit breaker 和 health check。
20. 任何不可恢复失败都必须进入 dead letter 或人工 repair workflow, 不允许静默丢弃。

---

## 13. 最小目录骨架

```text
agent_kernel/
  __init__.py
  app/
    __init__.py
    conversation_task_hub.py
    run_service.py
    approval_service.py
    artifact_service.py
    memory_service.py
  domain/
    __init__.py
    events.py
    states.py
    conversation.py
    task.py
    run.py
    workflow.py
    agent.py
    capability.py
    memory.py
    context.py
    artifact.py
    policy.py
  runtime/
    __init__.py
    engine.py
    scheduler.py
    lease.py
    checkpoint.py
    queue.py
    outbox.py
    dead_letter.py
    budget.py
    circuit_breaker.py
    health.py
    dispatcher.py
    control.py
    retry.py
    cancellation.py
    recovery.py
  workflow/
    __init__.py
    spec.py
    graph.py
    node.py
    edge.py
    command.py
    reducer.py
    plan_patch.py
    executors/
      __init__.py
      base.py
      llm.py
      tool.py
      agent.py
      human.py
      subworkflow.py
      memory.py
      transform.py
  agents/
    __init__.py
    spec.py
    session.py
    runtime.py
    loop.py
    mailbox.py
    interaction_fabric.py
    group_chat.py
    observer.py
    steering.py
    taskboard.py
    handoff.py
    team.py
    lineage.py
  capabilities/
    __init__.py
    registry.py
    runtime.py
    tool.py
    result.py
    workbench.py
    adapters/
      __init__.py
      local.py
      cli.py
      process.py
      http.py
      sdk.py
      mcp.py
      workflow.py
      agent.py
  models/
    __init__.py
    gateway.py
    provider.py
    request.py
    response.py
    stream.py
    tool_call.py
    capabilities.py
    cost.py
  memory/
    __init__.py
    facade.py
    working.py
    episodic.py
    semantic.py
    procedural.py
    artifact.py
    retrieval.py
    policy.py
    consolidation.py
  context/
    __init__.py
    manager.py
    budget.py
    packer.py
    compression.py
    ledger.py
    partitions.py
    token_counter.py
    density.py
    tool_visibility.py
    quality_gate.py
  autonomy/
    __init__.py
    planner.py
    explorer.py
    strategy.py
    verifier.py
    reflector.py
    trace_distiller.py
    workflow_induction.py
    workflow_library.py
    skill_evolution.py
    skill_service.py
    plan_patch_validator.py
  policy/
    __init__.py
    grant.py
    engine.py
    approval.py
    sandbox.py
    secrets.py
    egress.py
    audit.py
    masking.py
  persistence/
    __init__.py
    unit_of_work.py
    event_store.py
    state_store.py
    checkpoint_store.py
    artifact_store.py
    vector_store.py
    fts_store.py
    repositories.py
  observability/
    __init__.py
    trace.py
    audit.py
    cost.py
    metrics.py
    timeline.py
  evaluation/
    __init__.py
    replay.py
    golden_trace.py
    mocks.py
    suites.py
    assertions.py
  extensions/
    __init__.py
    manifest.py
    loader.py
    registry.py
    permissions.py
    compatibility.py
  hosts/
    __init__.py
    cli.py
    http.py
    event_stream.py
    dto.py
tests/
  unit/
  integration/
  replay/
```

---

## 14. MVP 验证场景

### 14.1 代码任务

流程:

```text
user request
  -> supervisor agent
  -> create implementation task
  -> spawn child agent
  -> child uses CLI/local tools
  -> run tests
  -> produce artifact/diff
  -> supervisor validates
  -> complete or retry
```

验证点:

- 子任务 lineage 清晰。
- tool call 有 event 和 artifact。
- 测试失败可 retry。
- cancel 可级联。

### 14.2 研究任务

流程:

```text
user question
  -> workflow retrieves sources
  -> artifacts stored
  -> agent summarizes
  -> episodic memory written
  -> context ledger explains selected evidence
```

验证点:

- Memory 与 Context 分离。
- Artifact 不塞进事件。
- Context Ledger 可解释。

### 14.3 长期任务

流程:

```text
objective
  -> taskboard decomposition
  -> worker claim
  -> heartbeat
  -> interrupt for approval
  -> resume
  -> review
  -> done
```

验证点:

- lease 生效。
- blocked reason 可见。
- approval 是正常状态。
- checkpoint 可恢复。

---

## 15. 后续编码建议

第一批代码不要从模型 provider 或复杂 Agent Loop 开始, 应先实现:

1. `domain` 模型与状态机。
2. `event_store` 和 `checkpoint_store`。
3. `workflow` 最小图执行。
4. `capability.runtime` 的 local fake tool。
5. `model.gateway` 的 fake provider。
6. 一个可 replay 的三节点 workflow 集成测试。

这能尽早验证架构最核心的假设: durable runtime、结构化 IO、事件引用、checkpoint/resume、policy hook 和 trace 是否能闭环。
