# Agent Kernel Development TODO

更新时间: 2026-06-07 00:55:20 CST

## 当前目标

从 `python-code-architecture-design.md` 出发，先搭建 Python Agent Kernel 的可运行代码骨架，再按阶段逐步实现稳定内核、工作流、能力运行时、Agent 编排、记忆上下文、治理回放和多 Agent 协作能力。

当前仓库状态: 已完成 Phase 0-9 的 MVP 主线；已具备 Durable Runtime、Agent Orchestration、Capability/Policy、Memory/Context、Replay/Observability、Extension SDK、Autonomy、Interaction Fabric 和 CLI Host 基础闭环。

重要边界: 当前实现是可运行 MVP 基座，不等价于 `python-code-architecture-design.md` 的完整目标。后续开发必须优先补齐实时干预、长期进程恢复、多 CLI AgentConnector、HTTP/event stream、MCP/Workbench、工作区隔离和自主探索深化等缺口，避免把“接口/草案/基础服务”误判为完整能力。

## 开发原则

- 先实现可测试、可恢复、可审计的最小内核，再扩展高级智能能力。
- Runtime 是唯一调度 Tool、SubWorkflow、Agent、Human 节点的组件。
- Event log、checkpoint、artifact ref 是长期任务恢复的基础，不把大 payload 塞入事件。
- Workflow 是显式可恢复执行图；Skill 是 Agent 可解释的过程知识，不能绕过 Runtime/Policy。
- 所有副作用能力必须经过 CapabilityRuntime、Policy、idempotency key 和 trace。
- 每个 public API 都要配套单元测试；外部模型、CLI、MCP、HTTP 都用 fake/mock。

## 阶段计划

### Phase 0 - 项目骨架与领域模型

目标: 建立 Python 包、测试框架、核心领域对象、状态机和最小事件模型。

- [x] 创建 Python 项目配置: `pyproject.toml`, package metadata, pytest 配置, ruff/mypy 基础配置。
- [x] 创建目录骨架: `agent_kernel/`, `tests/unit/`, `tests/integration/`, `tests/replay/`。
- [x] 实现 `agent_kernel.domain` 基础值对象: `EntityRef`, `ArtifactRef`, `MemoryRef`。
- [x] 实现 `RuntimeEvent` 和事件类型常量。
- [x] 实现 Conversation/Task/Run/Workflow/Agent/Capability/Memory/Context/Policy 领域模型。
- [x] 实现状态机校验: Run, NodeStep, Task, AgentSession, Approval, ToolCall, HumanIntervention, RecoveryJob, CircuitBreaker。
- [x] 实现 JSON 序列化/反序列化辅助。
- [x] 编写领域模型和状态机单元测试。
- [x] 验收: 所有领域对象可序列化；非法状态迁移会报错；事件 payload 不保存大内容。

### Phase 1 - Durable Runtime MVP

目标: 能持久化执行一个 3 节点 workflow，并支持 checkpoint/resume/cancel/retry。

- [x] 实现 SQLite schema 和 persistence adapter: event_store, state_store, checkpoint_store, artifact_store。
- [x] 实现 UnitOfWork 事务边界。
- [x] 实现 WorkflowSpec/Graph 校验: node、edge、start、condition。
- [x] 实现 `ExecutionCommand`, `NodeContext`, `NodeResult`, `NodeExecutor` 协议。
- [x] 实现 RuntimeEngine: create_run, start_run, execute_step, reduce_state, finish_run。
- [x] 实现 Scheduler 和 Lease 基础组件。
- [x] 实现 checkpoint 保存与从持久化状态继续执行。
- [x] 实现 run-level cancel 基础控制。
- [x] 实现 retry classifier、outbox、dead letter 基础组件。
- [x] RuntimeEngine 接入 retry/backoff 的重新调度。
- [x] RuntimeEngine 接入 idempotency key 去重，避免重复执行已成功 step。
- [x] 实现 budget manager 与 circuit breaker 基础状态流转。
- [x] RuntimeEngine 接入 budget exhausted pause/cancel。
- [x] RuntimeEngine 接入 circuit open pause。
- [x] 实现 mock model provider 和最小 local tool executor。
- [x] 编写集成测试: 3 节点 workflow、checkpoint resume、run cancel、不可恢复 step dead letter、retry 成功、预算暂停、circuit 暂停、幂等去重。
- [x] 验收: crash/restart 后可从 checkpoint 继续；不可恢复 step 进入 dead letter；幂等 step 不重复执行。

### Phase 2 - Agent Orchestration MVP

目标: 支持基础 Agent Loop、子 Agent 派生、mailbox 和任务血缘。

- [x] 实现 AgentSpec/AgentSession repository。
- [x] 实现 AgentLoop: build context, call model, persist turn。
- [x] 实现 child agent spawn/await/cancel 基础能力。
- [x] 实现 mailbox message routing。
- [x] 实现 session parent-child lineage 和 supervisor-worker 基础流程。
- [x] 编写测试: supervisor 创建 child agent、child 完成事件、child cancel、cascade cancel、agent workflow node。
- [x] 验收: Agent 可作为 workflow node 执行，子 Agent 输出可回传 supervisor。

### Phase 3 - Capability Runtime 与 Policy

目标: 外部能力统一封装，并对副作用执行做权限和审批控制。

- [x] 实现 CapabilityRegistry 和 CapabilityRuntime。
- [ ] 实现 ToolResult 标准 envelope。
- [x] 实现 LocalTool adapter。
- [x] 实现 Process adapter timeout 与 tool_call 状态。
- [x] 实现 Process adapter 外部 cancel/kill 控制。
- [x] 实现 CLI/Process adapter stdout/stderr stream 基础 async iterator。
- [x] 实现 Process adapter 可插拔隔离策略和 POSIX process group cancel/kill 基础接口。
- [x] 实现 MCP adapter 接口、fake、stdio JSON-RPC client 和 MCP tool executor。
- [x] 实现 Workbench 协议。
- [x] 实现通用 HTTP Workbench adapter，并接入 CapabilityRuntime policy/audit/tool_call 路径。
- [x] 实现浏览器/桌面/移动控制 ControlWorkbench 协议与 fake backend，并接入 CapabilityRuntime policy/audit/tool_call 路径。
- [x] 实现 TMWebDriver-compatible HTTP browser backend，支持 session list、execute_js、navigate。
- [x] 实现 ADB mobile backend，支持 devices、uiautomator dump 解析、tap、type_text、keyevent、screenshot。
- [x] 实现 Win32 desktop backend，支持窗口枚举、截图、物理坐标点击、快捷键、剪贴板粘贴输入。
- [x] 实现 DesktopUIDetector 协议与 UIA-style desktop tree detector，支持 dump_ui 节点归一化。
- [x] 实现 CapabilityGrant、ApprovalRequest、PolicyEngine 基础能力。
- [x] 实现 GrantStore 和 grant 过期检查。
- [x] 实现 Approval approve/reject resolution。
- [x] 实现 AuditLog 基础持久化。
- [x] 实现高风险 capability approval interrupt 基础流程。
- [x] 实现审批通过后 resume 当前 tool node。
- [x] 实现进程工具运行时 cancel/kill 基础流程。
- [x] 编写测试: grant 拒绝、approval interrupt、tool node success、approval resume、approval reject、expired grant、audit。
- [ ] 验收: 写文件、执行命令、网络访问都经过 policy check 和 audit。

### Phase 4 - Memory 与 Context

目标: 建立 Memory 与 Context 分离，并能解释每次上下文构建依据。

- [x] 实现 working memory。
- [x] 实现 episodic memory。
- [x] 实现 artifact memory。
- [x] 实现接口化 sparse semantic retrieval。
- [x] 实现结构化 fact conflict detection。
- [x] 实现 retrieval pack 基础对象和 context.built ledger。
- [x] 实现 context candidates、budget partition。
- [x] 实现 tool visibility pruning。
- [x] 实现 context ledger。
- [x] 实现长文本 artifact 引用，不直接进入 event payload。
- [x] 编写测试: context selection rationale、budget overflow。
- [x] 编写测试: sensitivity filtering。
- [x] 验收: Agent turn 能解释上下文包含/省略原因。

### Phase 5 - Observability、Replay、Evaluation

目标: 所有关键运行过程可追踪、可审计、可回放。

- [x] 实现 trace timeline。
- [x] 实现 cost ledger。
- [ ] 实现 audit sink。
- [x] 实现 exact replay。
- [x] 实现 partial/recovery replay 草案。
- [x] 实现 eval suite 基础接口。
- [x] 编写测试: replay 不调用真实模型/工具即可复现 timeline。
- [x] 验收: 每个 run 可以 inspect timeline、cost、event、checkpoint。
- [x] 验收: 每个 run 可以 inspect artifact。

### Phase 6 - Extension SDK

目标: 通过 manifest 注册 provider/tool/workflow/context strategy。

- [x] 实现 ExtensionManifest。
- [x] 实现 ManifestLoader。
- [x] 实现 ContributionRegistry。
- [x] 实现 permission declaration 到 PolicyEngine 的接入。
- [x] 编写测试: 插件注册工具 provider、权限声明生效。
- [x] 验收: 新能力可通过 manifest 注册，不修改内核代码。

### Phase 7 - Autonomous Exploration 与 Skill Evolution

目标: 支持开放任务自主探索，并把成功链路沉淀为可复用 workflow/skill。

- [x] 实现 ExplorationTask、CandidateStrategy、ExplorationAttempt。
- [x] 实现 planner/explorer/verifier 基础接口。
- [x] 实现 StrategyGenerator 协议和 CompositeExplorationPlanner 多策略生成。
- [x] 实现 reflector 协议与 deterministic failure reflection。
- [x] 实现 trace distiller。
- [x] 实现 WorkflowTemplate 和 WorkflowLibrary。
- [x] 实现子工作流 WorkflowSpec 注册、ref 解析和条件校验闭环。
- [x] 实现 SkillCard、SkillService、PlanPatchValidator。
- [x] 实现 WorkflowPatchApplier，支持 add_node/add_edge/set_start 的保守 workflow patch 应用。
- [x] 实现 SkillEvolutionRecord 基础记录。
- [x] 实现 GoldenTrace 到 draft WorkflowTemplate 的归纳流程。
- [x] 编写测试: 成功 trace 归纳、失败探索、skill evolution 记录。
- [x] 编写测试: 失败反思、PlanPatch 校验、WorkflowPatchApplier。
- [x] 编写测试: 多策略探索。
- [x] 验收: 开放任务可在预算内探索；成功路径可发布为 draft subworkflow。

### Phase 8 - Multi-Agent Interaction Fabric

目标: 支持群聊、多 CLI Agent 持续会话、多对多协作、观察者辅助纠偏。

- [x] 实现 InteractionChannel 和 InteractionMessage。
- [x] 实现 GroupChatSession、SpeakerPolicy、DiscussionTurn。
- [x] 实现 AgentPool 和 pool scheduler。
- [x] 实现 persistent CLI AgentConnector 协议占位和 fake。
- [x] 实现多 session AgentConnectorRouter，将 participant 路由到不同 connector/session，并把 turn 输出写回 channel。
- [x] 实现 StructuredStdioAgentConnector，使用 JSONL 结构化协议连接长驻 CLI shim。
- [x] 实现 ProductCLIConnectorFactory，通过产品 shim spec 构建 structured stdio connectors。
- [x] 实现 TaskBoard 基础。
- [x] 实现 handoff。
- [x] 实现 ObservationFinding 和 observer request_pause 基础。
- [x] 实现 Observer request_pause 可选驱动 Runtime pause，并持久化 run.paused event/checkpoint。
- [x] 实现 channel summary 和 decision artifact 基础服务。
- [x] 实现 cross-channel summary 和 decision artifact 聚合基础服务。
- [x] 实现自由发言和主持人选人 speaker selector。
- [x] 实现 workspace isolation、merge/review workflow 草案。
- [x] 实现真实 Git worktree allocation、release、approved patch merge 和 merge conflict rollback。
- [x] 编写测试: 回合制群聊、observer request_pause。
- [x] 编写测试: 自由发言、主持人选人策略。
- [x] 编写测试: human intervention、fake connector routing。
- [x] 编写测试: fake 多 connector session routing。
- [x] 编写测试: 真实子进程 JSONL CLI session connector。
- [x] 编写测试: 多产品 CLI shim config routing。
- [ ] 验收: 多个 Codex/Claude/Gemini CLI session 可被协调推进同一个项目任务。

### Phase 9 - Hosts 与日常使用形态

目标: 提供可用的 CLI Host，并为 Web/Desktop task workspace 预留 API。

- [x] 实现 CLI: sample-run/inspect/replay/cancel/approve/reject。
- [x] 实现 CLI: llm-smoke 真实 LLM 配置验证入口。
- [x] 实现 CLI: intervene。
- [x] 实现 HTTP API 草案: task、run、artifact、approval、event stream route DTO。
- [x] 实现 event stream DTO。
- [x] 实现只读 HTTP host: run inspect、run event NDJSON stream、artifact inspect。
- [x] 实现 HTTP 写控制 host: run cancel、human intervention、approval approve/reject、tool-call cancel/kill。
- [x] 实现 task workspace 数据结构 DTO: conversation、taskboard、agent sessions、channels、artifacts、runtime controls。
- [x] 编写测试: CLI smoke。
- [x] 编写测试: HTTP DTO serialization、event stream。
- [x] 验收: 日常可通过 CLI 发起任务，通过 inspect/replay 观察运行过程。

## 第一轮实现范围

本轮先完成 Phase 0 的代码骨架和测试基础，不直接进入复杂 runtime。

- [x] 初始化项目配置。
- [x] 创建 package 目录。
- [x] 实现领域基础模型和值对象。
- [x] 实现状态机校验。
- [x] 写第一批单元测试。
- [x] 运行测试并修复失败。

下一轮实现范围: Phase 1 Durable Runtime 的最小闭环。

- [x] 设计 SQLite 最小 schema。
- [x] 实现 `EventStore`, `CheckpointStore`, `StateStore`。
- [x] 实现 `UnitOfWork` 事务边界。
- [x] 实现 3 节点 workflow 的 RuntimeEngine 执行闭环。
- [x] 编写 checkpoint/resume/cancel 集成测试。

下一轮实现范围: Phase 1 Durable Runtime 的可靠性补强。

- [x] 实现 `Scheduler` 和 `Lease`。
- [x] 实现 step 级状态存储。
- [x] 实现 retry classifier 和 backoff decision。
- [x] 实现 dead letter。
- [x] 实现 outbox。
- [x] 实现 budget manager。
- [x] 实现 circuit breaker。
- [x] 增加不可恢复失败、dead letter、可靠性组件测试。

下一轮实现范围: Phase 1 Durable Runtime 的执行语义补强。

- [x] RuntimeEngine 接入 retry/backoff 的重新调度，而不是只分类。
- [x] RuntimeEngine 接入 idempotency key 去重，避免重复副作用。
- [x] RuntimeEngine 接入 budget manager，超预算 pause/cancel。
- [x] RuntimeEngine 接入 circuit breaker，circuit open 时暂停 run。
- [x] 实现最小 mock model provider。
- [x] 实现最小 local tool executor。
- [x] 增加失败重试、幂等去重、预算耗尽、circuit open 集成测试。

下一轮实现范围: Phase 2 Agent Orchestration MVP。

- [x] 实现 `AgentSession` repository。
- [x] 实现 mailbox 基础路由。
- [x] 实现 AgentLoop 最小流程: build context -> call model -> persist turn。
- [x] 实现 supervisor 创建 oneshot child agent。
- [x] 实现 child agent 完成事件回传 supervisor trace。
- [x] 实现 cancel child agent 的基础语义。
- [x] 编写 Agent orchestration 单元/集成测试。

下一轮实现范围: Phase 2 Agent Orchestration 深化。

- [x] AgentLoop 解析结构化 `ExecutionCommand`。
- [x] AgentLoop 支持可选 SkillContextProvider，自动选择 active skill 注入模型上下文。
- [x] Agent 作为 workflow node 执行。
- [x] child agent 输出以结构化 result 回传 supervisor。
- [x] 实现 session parent-child lineage。
- [x] 实现 cancel 级联到所有 child sessions。
- [x] 增加 supervisor-worker / agent workflow node 集成测试。

下一轮实现范围: Phase 3 Capability Runtime 与 Policy。

- [x] 实现 `CapabilityRegistry`。
- [x] 实现 `CapabilityRuntime`。
- [x] 将 `LocalToolExecutor` 接入 capability runtime。
- [x] 实现最小 `PolicyEngine` 与 grant 检查。
- [x] 实现 approval request / interrupt 基础流程。
- [x] 实现 tool node executor 通过 CapabilityRuntime 调用工具。
- [x] 增加 policy deny、approval interrupt、tool result persistence 测试。

下一轮实现范围: Phase 3 Governance 深化。

- [x] 实现 grant 持久化与过期检查。
- [x] 实现 approval approve/reject resolution。
- [x] RuntimeEngine 支持 approval approved 后 resume 当前 tool node。
- [x] 实现 audit log sink。
- [x] 实现 CLI/Process adapter 的 timeout/cancel/kill 基础。
- [x] 增加审批通过恢复、审批拒绝、expired grant、audit 记录测试。

下一轮实现范围: Phase 3 CLI/Process Adapter 与 ToolCall 控制。

- [x] 实现 `ProcessToolExecutor`。
- [x] 支持 timeout。
- [x] 支持 process group cancel/kill 基础接口。
- [x] 实现 `ToolCallRecord` 和 tool call 状态持久化。
- [x] 将危险命令通过 approval gate 拦截，审批前不执行。
- [x] 增加 timeout、危险命令审批前不执行测试。
- [x] 增加 cancel、kill 测试。

下一轮可选范围 A: Phase 3 外部 ToolCall 控制收尾。

- [x] 为 long-running process 建立 active process registry。
- [x] 支持外部 cancel tool call。
- [x] 支持直接 kill。
- [x] 支持 grace period 后 kill。
- [x] 持久化 `cancelling/cancelled/killing/killed` 状态。
- [x] 增加外部 cancel/kill 集成测试。

下一轮可选范围 B: Phase 4 Memory 与 Context。

- [x] 实现 working memory。
- [x] 实现 artifact memory。
- [x] 实现 retrieval pack 基础。
- [x] 实现 context budget 和 context ledger。
- [x] AgentLoop 接入 ContextManager。

下一轮实现范围: Phase 4 Context 深化。

- [x] sensitivity filtering。
- [x] tool visibility pruning。
- [x] 长文本 memory/artifact 自动转 artifact ref，避免 event payload 超限。
- [x] context compression strategy 基础: `omit_over_budget`。
- [x] context density scoring 更细化。
- [x] 增加 sensitivity/tool visibility/large artifact 测试。

下一轮可选范围: Phase 5 Observability、Replay、Evaluation。

- [x] 实现 trace timeline 查询。
- [x] 实现 cost ledger。
- [x] 实现 exact replay 基础。
- [x] 实现 replay fake model/tool 外部调用计数断言。
- [x] 增加 replay 不调用真实 model/tool 测试。

下一轮实现范围: Phase 5 深化。

- [x] artifact inspect 聚合。
- [x] partial replay。
- [x] recovery replay。
- [x] eval suite 基础接口。
- [x] replay assertions。
- [ ] cost regression 测试。

下一轮可选范围: Phase 6 Extension SDK。

- [x] 实现 ExtensionManifest loader。
- [x] 实现 ContributionRegistry。
- [x] 插件声明权限接入 PolicyEngine。
- [x] 插件注册 capability provider 测试。

下一轮实现范围: Phase 7 Autonomous Exploration 与 Skill Evolution。

- [x] 实现 ExplorationTask、CandidateStrategy、ExplorationAttempt repository/service。
- [x] 实现 planner 生成候选策略。
- [x] 实现 explorer 执行 attempt。
- [x] 实现 verifier。
- [x] 实现 trace distiller。
- [x] 实现 WorkflowTemplate library。
- [x] 增加成功 trace 归纳测试。

下一轮实现范围: Phase 8 Multi-Agent Interaction Fabric。

- [x] 实现 InteractionChannel 和 InteractionMessage。
- [x] 实现 GroupChatSession、SpeakerPolicy、DiscussionTurn。
- [x] 实现 AgentPool 和 pool scheduler。
- [x] 实现 TaskBoard 基础。
- [x] 实现 ObservationFinding 和 observer request_pause。
- [x] 增加回合制群聊、observer request_pause 测试。

下一轮实现范围: Phase 9 Hosts 与日常使用形态。

- [x] 实现 CLI: run sample workflow。
- [x] 实现 CLI: inspect timeline。
- [x] 实现 CLI: replay run。
- [x] 实现 CLI: approve/reject approval。
- [x] 实现 CLI: cancel run。
- [x] 实现 CLI: cancel/kill tool call 控制请求。
- [x] 实现 HTTP DTO 草案。
- [x] 增加 CLI smoke tests。

下一轮建议范围: 收敛与工程化。

- [x] 增加 README 使用说明。
- [x] 增加 console script entry point。
- [x] 增加 OpenAI-compatible LLM provider、环境变量/配置文件读取和 smoke 验证命令。
- [x] 增加 TODO 已实现能力真实场景验收脚本: `scripts/verify_realized_todo.py`。
- [x] 补非电脑控制/浏览器/GUI 操作类能力: HumanIntervention、SkillService、PlanPatchValidator、MCP stdio/fake、Workbench fake、AgentConnector fake、host DTO。
- [ ] 清理 `.DS_Store` 和 `__pycache__` 工作区噪音。
- [x] 跑 `python -m compileall`。
- [ ] 如允许安装 dev 依赖，跑 ruff/mypy/pytest。
- [x] 梳理 MVP 缺口清单: HTTP host、persistent CLI connector、真实 Workbench adapter、workspace isolation、长期 recovery。

## 架构对照复核

复核时间: 2026-06-06 22:49:05 CST

结论: 当前代码可运行、测试通过，适合作为后续开发地基；但完整架构仍有若干关键能力未实现或仅为 MVP 浅实现。开发排期应按下列缺口推进。

### 已正确落地的 MVP 基座

- [x] Python package、领域模型、状态机、JSON 序列化和基础测试已建立。
- [x] SQLite event/state/checkpoint/artifact 持久化、RuntimeEngine、WorkflowGraph、NodeExecutor 协议已可运行。
- [x] checkpoint resume、run cancel、retry、dead letter、budget pause/cancel、circuit pause、基础 idempotency 已接入 RuntimeEngine。
- [x] AgentSession、Mailbox、AgentLoop、child agent spawn/await/cancel、agent-as-workflow-node 已具备基础闭环。
- [x] CapabilityRuntime、PolicyEngine、ApprovalService、GrantStore、AuditStore、Local/Process tool adapter 已具备基础闭环。
- [x] Working/artifact memory、ContextManager、context ledger、sensitivity filtering、tool visibility pruning、长文本 artifact ref 已实现。
- [x] Trace timeline、cost ledger、artifact inspect、exact/partial/recovery replay、eval assertions 已实现基础能力。
- [x] Extension manifest loader、ContributionRegistry、permission-to-grant mapping 已实现。
- [x] Autonomous exploration、trace distillation、draft workflow template、SkillService、PlanPatchValidator、skill evolution record 已有最小闭环。
- [x] Interaction channel、message、round-robin group chat、agent pool、taskboard、observer finding 已有基础服务。
- [x] HumanInterventionService、MCP stdio/fake、Workbench fake、AgentConnector fake、host DTO 已有接口级闭环。
- [x] CLI host 已支持 sample-run、inspect、replay、approve、reject、cancel run、cancel/kill tool call、intervene、llm-smoke。
- [x] HTTP host 已支持 run inspect、run events NDJSON、artifact inspect、cancel run、intervene、approve/reject、cancel/kill tool call。

### 与完整设计不一致或深度不足

- [ ] HumanIntervention 已支持 CLI/HTTP、事件、working memory 和 `pause_and_resume` interrupt；仍缺 `cancel_current_step_and_resume` 和更细的 context priority partition。
- [ ] ToolCall 实时控制已支持 CLI/DTO 控制请求和当前进程内 active registry；进程重启后无法 cancel/kill 已运行子进程。
- [ ] Process adapter 已实现 stdout/stderr stream 基础 async iterator、可插拔隔离策略和 POSIX process group cancel/kill；仍缺结构化终端协议、跨重启 reattach/control、Windows Job Object isolation 和 `tool.call.cancelled/killed/failed` 完整事件语义。
- [ ] Policy/Audit 只覆盖 CapabilityRuntime 调用路径；尚未证明所有写文件、执行命令、网络访问都统一经过 policy check、grant、approval、audit 和 idempotency。
- [ ] MCP/Workbench/CLI AgentConnector 已有协议、fake、MCP stdio JSON-RPC client、MCP tool executor、通用 HTTP Workbench adapter、多 session router、structured JSONL stdio connector 和 product CLI connector factory；ControlWorkbench 已覆盖 browser JS、desktop click/key/screenshot/dump_ui、mobile UI/tap/text 原子能力入口，并已有 TMWebDriver-compatible HTTP browser backend、Win32 desktop backend、UIA-style desktop tree detector 和 ADB mobile backend；具体 UIA provider、视觉检测 adapter、Codex/Claude/Gemini 产品 CLI shim 尚未实现。
- [ ] 多 Agent 协作已有基础 channel/round-robin/free-for-all/moderator-select/taskboard/handoff/connector routing/channel + cross-channel decision artifact；仍缺具体 Codex/Claude/Gemini 产品 shim。
- [ ] Workspace isolation 已有接口协议、fake backend、Git worktree 分配/release、approved patch merge 执行和冲突 rollback；仍缺多 agent merge queue、冲突自动修复 workflow 和更完整 review policy。
- [ ] Observer request_pause 已可选驱动 Runtime pause 并持久化 event/checkpoint；仍缺上下文纠偏和当前 step 中止。
- [ ] Autonomous exploration 已有可组合多策略 planner + 注入式 executor，并已补 PlanPatchValidator、SkillService 和 deterministic failure reflector；仍缺动态工具/工作流组合。
- [x] Skill 与 Workflow 的互调规则已有基础服务、Agent 自动选择 skill、compiled workflow 注册/解析和 workflow patch 应用闭环。
- [ ] Memory 已有 episodic memory、deterministic retrieval、高重要度 episode 到 semantic 的基础 consolidation、sparse semantic retrieval 和结构化 fact conflict detection；仍缺外部 vector store-backed retrieval、后台长期 consolidation 和复杂事实归并策略。
- [ ] Recovery 已有 stale step scanner、event/checkpoint/artifact consistency check 和保守 metadata repair；仍缺复杂 artifact/event repair、不可恢复对象 repair workflow 和真实 worker 接管闭环。
- [ ] HTTP/event stream/workspace DTO 已实现；HTTP host 已支持 run inspect、event NDJSON stream、artifact inspect、run/tool-call 控制、审批和人工干预；SSE/WebSocket event stream、task create 和 Web/Desktop 工作台仍未实现。
- [ ] Extension SDK 目前只注册 metadata，不动态 import/执行 extension entrypoint；还不是完整插件运行时。

### 下一阶段建议优先级

- [x] P0: 实现 HumanInterventionService + CLI `intervene`，支持追加纠偏事件、更新 working memory、pause/interrupted 当前 run。
- [x] P0: 实现 CLI/API tool-call cancel/kill 控制请求，并补齐 ToolCall event 写入与测试。
- [x] P0: 实现持久化 recovery scanner，处理 stale lease/crash 后未完成 step 的重调度或 dead-letter。
- [x] P0: 补 event/checkpoint/artifact 一致性检查。
- [x] P0: 补 event/checkpoint/artifact 保守自动 repair workflow: restore missing RunState from checkpoint, align RunState checkpoint pointer。
- [ ] P0: 补复杂 artifact/event repair workflow 与不可恢复对象 repair workflow。
- [x] P1: 定义 persistent CLI AgentConnector 协议，用 fake connector 先跑通 connector routing 测试。
- [x] P1: 实现 workspace isolation 草案: allocator/review backend 协议、artifactized patch、review/merge workflow。
- [x] P1: 实现真实 git worktree allocation、release、merge 执行和冲突 rollback。
- [x] P1: 补 MCP adapter fake/stdio client/tool executor、Workbench protocol interface、stream DTO。
- [ ] P1: 补 Exploration 动态组合能力；多策略探索、PlanPatchValidator、SkillService、deterministic reflector 已完成基础版。
- [x] P2: 实现 HTTP route DTO 和 event stream DTO，为 Web/Desktop task workspace 做数据面准备。
- [x] P2: 补 episodic memory 和 deterministic retrieval/consolidation 接口。
- [ ] P2: 补外部 vector store-backed retrieval、background long-term consolidation、复杂事实归并策略；sparse semantic retrieval 和结构化 fact conflict detection 已完成基础版。

## 进度日志

### 2026-06-06 21:04:39 CST

- 创建开发 TODO。
- 明确从 Phase 0 开始，不跳过领域模型和状态机。
- 下一步: 初始化 Python 项目骨架和测试配置。

### 2026-06-06 21:13:47 CST

- 完成 Python 项目配置和 package 骨架。
- 完成 `agent_kernel.domain` 第一版领域模型: event、state、conversation、task、run、workflow、agent、capability、memory、context、policy、stability、skill、autonomy、extension。
- 完成状态机迁移校验。
- 完成标准库 `unittest` 单元测试 18 个。
- 验证命令: `python3 -m unittest discover -s tests`，结果 18 passed。
- 下一步: 进入 Phase 1，先实现 SQLite event/checkpoint/state persistence。

### 2026-06-06 21:23:26 CST

- 完成 SQLite schema 与 persistence adapter: `EventStore`, `StateStore`, `CheckpointStore`, `ArtifactStore`。
- 完成 `UnitOfWork` 事务边界和 rollback 测试。
- 完成 `WorkflowGraph`, `NodeExecutorRegistry`, `FunctionNodeExecutor`, `reduce_state`。
- 完成 `RuntimeEngine` 最小闭环: `create_run`, `run_until_waiting`, checkpoint 持久化继续执行, run-level cancel。
- 完成集成测试: 3 节点 workflow 执行完成、engine 重建后继续执行、run cancel 事件和 checkpoint。
- 验证命令: `python3 -m unittest discover -s tests`，结果 25 passed。
- 下一步: 补 Phase 1 可靠性能力，优先 `Scheduler`/`Lease`、step 状态、retry、dead letter。

### 2026-06-06 21:34:19 CST

- 修正 step 模型边界: `NodeStepRecord` 下沉到 `domain.step`，避免 `persistence` 与 `runtime` 循环依赖。
- 完成 node step 持久化: `StepStore`。
- 完成 dead letter 持久化: `DeadLetterStore`。
- 完成 transactional outbox 基础持久化: `OutboxStore`。
- 完成 runtime 可靠性基础组件: `Lease`, `Scheduler`, `RetryClassifier`, `BudgetManager`, `CircuitBreaker`。
- RuntimeEngine 接入 step 生命周期记录: scheduled/leased/running/succeeded/failed。
- RuntimeEngine 在不可重试 step 失败时写入 dead letter。
- 新增测试覆盖 reliability persistence、scheduler/lease、retry、budget、circuit breaker、失败 dead letter。
- 验证命令: `python3 -m unittest discover -s tests`，结果 32 passed。
- 下一步: 让 retry、budget、circuit breaker、idempotency 从“组件可用”升级为“RuntimeEngine 执行语义”。

### 2026-06-06 21:39:21 CST

- RuntimeEngine 接入 retry/backoff 重新调度: transient/model/tool/system 失败可在同一 node 自动重试。
- RuntimeEngine 接入 stable idempotency key: 已成功的 `run_id:node_id:attempt` 不重复执行。
- RuntimeEngine 接入 budget exhausted pause/cancel 基础语义。
- RuntimeEngine 接入 circuit open pause 基础语义。
- 新增最小 `ModelGateway` 与 `MockModelProvider`。
- 新增最小 `LocalToolExecutor`。
- 新增测试覆盖 retry 后成功、预算暂停、circuit open 暂停、幂等去重、mock model/local tool。
- 验证命令: `python3 -m unittest discover -s tests`，结果 38 passed。
- 下一步: 进入 Phase 2 Agent Orchestration MVP。

### 2026-06-06 21:43:40 CST

- 新增 `MailboxMessage`, `AgentTaskResult`, `MailboxMessageStatus` 领域对象。
- 新增 `AgentSessionStore` 与 `MailboxStore`，扩展 SQLite schema。
- 新增 `AgentSessionService`，支持 create/get/complete/cancel session。
- 新增 `AgentLoop`，支持读取 mailbox、构造 `ModelContext`、调用 `ModelGateway`、持久化 turn 完成事件。
- 新增 `SupervisorService`，支持 spawn child、await child、cancel child。
- 新增 agent session 生命周期事件: `agent.session.created`, `agent.turn.completed`, `agent.session.cancelled`。
- 修正 Agent 状态机，允许 `starting -> cancelled/failed`。
- 新增测试覆盖 agent session/mailbox round trip、supervisor spawn child、child run once complete、cancel child。
- 验证命令: `python3 -m unittest discover -s tests`，结果 41 passed。
- 下一步: Phase 2 深化，优先 Agent 作为 workflow node、结构化 command parser、child result 回传和 task lineage。

### 2026-06-06 21:47:15 CST

- 新增 `AgentTaskResultStore`，AgentLoop 完成时持久化结构化 child result。
- `SupervisorService` 支持 `get_child_result` 和 `cancel_children`。
- AgentLoop 支持解析模型返回的结构化 `ExecutionCommand`。
- 新增 `AgentNodeExecutor`，Agent 可以作为 workflow node 被 RuntimeEngine 调度。
- 新增 agent workflow node 集成测试。
- 新增 cascade cancel children 集成测试。
- 验证命令: `python3 -m unittest discover -s tests`，结果 44 passed。
- 下一步: 进入 Phase 3 Capability Runtime 与 Policy。

### 2026-06-06 21:51:59 CST

- 新增 `CapabilityRegistry`。
- 新增 `CapabilityRuntime`，统一执行 capability 前调用 `PolicyEngine`。
- 新增 `PolicyEngine`, `PolicyDecision`, `PolicyDecisionType`。
- `LocalToolExecutor` 接入 CapabilityRuntime。
- 新增 `ToolNodeExecutor`，workflow tool node 通过 CapabilityRuntime 调用工具。
- 新增 `ApprovalStore` 与 `ApprovalService`，高风险 capability 可创建 pending approval request。
- RuntimeEngine 修正 `request_approval`/`interrupt` 语义: 保留当前 node，不提前推进 edge。
- 新增测试覆盖 capability allow、missing grant deny、高风险 approval interrupt、tool workflow success、approval request persistence。
- 验证命令: `python3 -m unittest discover -s tests`，结果 50 passed。
- 下一步: Phase 3 深化，补 approval resolve/resume、grant 持久化、audit、CLI/Process adapter。

### 2026-06-06 21:56:02 CST

- 新增 `GrantStore`，支持 run-scoped capability grant 持久化。
- 新增 `AuditStore` 与 `AuditRecord`，CapabilityRuntime 每次 policy check 写 audit。
- `PolicyEngine` 支持 grants provider，并忽略过期 grant。
- `ApprovalService` 支持 approve/reject；approve 会创建短期 run-scoped grant。
- 审批通过后可 `resume_from_checkpoint` 并重试当前 tool node。
- 新增测试覆盖 approval approve 创建 grant、reject 不创建 grant、expired grant 不生效、approval 后 resume 成功、audit 记录 require_approval/allow。
- 验证命令: `python3 -m unittest discover -s tests`，结果 54 passed。
- 下一步: Phase 3 CLI/Process adapter 和 tool call 运行时控制。

### 2026-06-06 22:00:06 CST

- 新增 `ToolCallRecord` 领域对象。
- 新增 `ToolCallStore`，持久化 tool call requested/running/succeeded/failed 状态。
- 新增 `ProcessToolExecutor`，支持 subprocess 执行与 timeout；超时先 terminate，必要时 kill。
- CapabilityRuntime 接入 tool call 状态持久化。
- CapabilityRuntime 支持 process adapter，找不到 process tool 时回退 local tool。
- 高风险 capability 在 approval 前不会执行 adapter。
- 新增测试覆盖 process success、process timeout、tool call success persistence、workflow process timeout failure、approval 前不执行工具。
- 验证命令: `python3 -m unittest discover -s tests`，结果 58 passed。
- 下一步: 可继续补外部 cancel/kill，或进入 Phase 4 Memory/Context。

### 2026-06-06 22:04:16 CST

- `ProcessToolExecutor` 增加 active process registry。
- 支持按 `tool_call_id` 外部 cancel，先 terminate，超过 grace period 后 kill。
- 支持按 `tool_call_id` 外部 kill。
- `CapabilityRuntime` 新增 `cancel_tool_call` 和 `kill_tool_call`。
- ToolCallStore 持久化 `cancelling/cancelled/killing/killed` 状态。
- 新增外部 cancel/kill 集成测试。
- 验证命令: `python3 -m unittest discover -s tests`，结果 60 passed。
- 下一步: 进入 Phase 4 Memory 与 Context。

### 2026-06-07 01:12:00 CST

- `ProcessToolExecutor` 新增 `ProcessIsolationStrategy` 协议，默认 `SingleProcessIsolationStrategy` 保持单进程行为。
- 新增 `PosixProcessGroupIsolationStrategy`，通过 new session + process group signal 支持父子进程树 cancel/kill。
- `ProcessToolExecutor.register` 支持 per-command isolation strategy override，executor 不暴露平台细节给 Runtime/CapabilityRuntime。
- 新增 POSIX 子进程树 cancel 测试，验证 cancel 会通知父进程创建的子进程。

### 2026-06-06 22:07:47 CST

- 新增 `MemoryStore`，支持按 scope/type 持久化和检索 memory。
- 新增 `MemoryFacade`，支持 working memory 和 artifact memory 写入。
- 新增 `ContextBudget`, `estimate_tokens`, `ContextManager`。
- ContextManager 支持构建 memory candidates、按 budget 选择/省略、生成 `ModelContext`。
- ContextManager 通过 `context.built` RuntimeEvent 写 context ledger。
- AgentLoop 支持可选注入 ContextManager，模型上下文可包含 memory refs 与 inclusion rationale。
- 新增测试覆盖 memory round trip、context selection/omission、context ledger、AgentLoop 使用 memory context。
- 验证命令: `python3 -m unittest discover -s tests`，结果 63 passed。
- 下一步: 可继续 Phase 4 深化 sensitivity/tool visibility/large artifact，或进入 Phase 5 Replay。

### 2026-06-06 22:12:59 CST

- ContextManager 支持 `allowed_sensitivities`，默认只允许 `public/internal`。
- ContextManager 支持 `available_tools` + `tool_allowlist`，写入 `tool_visibility` 和 `tool_visibility_pruned` warning。
- MemoryFacade 对长文本 memory 自动生成 artifact ref，并把 inline content 缩成 artifact 摘要。
- ContextManager 把选中 memory 的 artifact refs 放入 `ModelContext.attachments` 和 `RetrievalPack.artifact_refs`。
- ContextPlan 支持 `compression_strategy='omit_over_budget'` 和改进后的 density score。
- 新增测试覆盖 sensitivity filtering、tool visibility pruning、large memory artifact ref。
- 验证命令: `python3 -m unittest discover -s tests`，结果 66 passed。
- 下一步: 进入 Phase 5 Observability、Replay、Evaluation。

### 2026-06-07 01:24:00 CST

- 新增 `SemanticRetriever`、`SemanticQuery`、`SemanticSearchResult` 协议/DTO。
- 新增 `SparseSemanticRetriever`，用标准库稀疏向量余弦相似度提供可运行 semantic retrieval 基础版。
- 新增 `FactConflictDetector`、`FactStatement`、`FactConflict` 和 `StructuredFactConflictDetector`。
- `MemoryFacade` 新增 `write_semantic`、`retrieve_semantic`、`detect_fact_conflicts`，支持写入 semantic fact 时附加 conflict metadata。
- 新增测试覆盖 semantic retrieval 排序、结构化事实冲突检测和 conflict metadata 写回。

### 2026-06-06 22:20:22 CST

- 新增 `TraceService`, `TraceTimeline`, `TimelineEntry`。
- Trace timeline 聚合 runtime events、node steps、tool calls、audit records。
- 新增 `CostService`, `CostLedger`，从 usage payload 和 tool calls 聚合成本/调用数。
- 新增 `ReplayService`, `ReplayResult`，exact replay 从 event log 和 latest checkpoint 复现 run 结果。
- 新增测试覆盖 timeline 查询、cost ledger、exact replay 不调用真实 tool。
- 验证命令: `python3 -m unittest discover -s tests`，结果 69 passed。
- 下一步: Phase 5 深化 partial/recovery replay 和 eval suite，或进入 Phase 6 Extension SDK。

### 2026-06-06 22:23:33 CST

- 新增 `ArtifactInspectionService`，从 run state 和 runtime events 聚合 artifact refs，并报告 missing artifacts。
- ReplayService 新增 `partial_replay` 和 `recovery_replay`。
- 新增 `ReplayAssertions`，支持 run status、event exists、no external calls。
- 新增 `ReplayEvalSuite` 和 `EvalSuiteResult`。
- 新增测试覆盖 artifact inspect、partial replay、recovery replay、replay assertions、eval suite。
- 验证命令: `python3 -m unittest discover -s tests`，结果 70 passed。
- 下一步: 进入 Phase 6 Extension SDK。

### 2026-06-06 22:25:54 CST

- 新增 `ExtensionManifestLoader`，支持 dict/json manifest 加载。
- 新增 `ContributionRegistry`，保存 manifest contributions。
- ContributionRegistry 支持将 `tool_provider` contribution 注册为 `CapabilitySpec` 元数据。
- 新增 `ExtensionPermissionMapper`，将 `capability:*` permission 映射为 run-scoped `CapabilityGrant`。
- 新增测试覆盖 manifest dict/json 加载、tool capability metadata 注册、extension permission grant 接入 PolicyEngine。
- 验证命令: `python3 -m unittest discover -s tests`，结果 73 passed。
- 下一步: 进入 Phase 7 Autonomous Exploration 与 Skill Evolution。

### 2026-06-06 22:30:46 CST

- 新增 `AutonomyStore`，统一持久化 exploration/strategy/attempt/golden_trace/workflow_template/skill_evolution。
- 新增 `ExplorationPlanner`, `ExplorationExecutor`, `ExplorationVerifier`。
- 新增 `TraceDistiller`，从成功 attempt 生成 `GoldenTrace`。
- 新增 `WorkflowLibrary`，可发布 draft `WorkflowTemplate`，并支持注册/解析已校验的 `WorkflowSpec`。
- 新增 `ExplorationService`，打通 create task -> plan -> attempt -> verify -> distill -> publish draft workflow。
- 新增 `SkillEvolutionService`，记录 `compile_workflow` 演化决策。
- 修复 ProcessToolExecutor kill/cancel 竞态: external status 先写入再等待进程退出。
- 新增测试覆盖成功探索发布 draft workflow、失败探索、skill evolution 记录。
- 验证命令: `python3 -m unittest discover -s tests`，结果 76 passed。
- 下一步: 进入 Phase 8 Multi-Agent Interaction Fabric。

### 2026-06-06 22:35:12 CST

- 新增 `domain.interaction`，包含 participant、channel、message、speaker policy、group chat、discussion turn、agent pool、taskboard item、observation finding。
- 新增 `InteractionStore`，持久化 interaction records。
- 新增 `InteractionFabric`，支持 channel 创建和消息发送/读取。
- 新增 `GroupChatService`，支持 round-robin speaker 和 discussion turns。
- 新增 `AgentPoolScheduler`，支持基础 pool selection。
- 新增 `TaskBoardService`，支持创建和分配任务项。
- 新增 `ObserverService`，支持 observer request_pause finding。
- 新增测试覆盖 channel message、round-robin group chat、agent pool selection、taskboard assign、observer request_pause。
- 验证命令: `python3 -m unittest discover -s tests`，结果 79 passed。
- 下一步: 进入 Phase 9 Hosts 与日常使用形态。

### 2026-06-06 22:38:38 CST

- 新增 `hosts.dto`，提供 host response helpers。
- 新增 `hosts.cli`，支持 `sample-run`, `inspect`, `replay`, `approve`, `reject`, `cancel`。
- CLI `sample-run` 可运行内置 sample workflow 并持久化 run。
- CLI `inspect` 可输出 trace timeline。
- CLI `replay` 可输出 exact replay 摘要。
- CLI approval commands 可 approve/reject pending approval。
- 新增 CLI smoke tests。
- 验证命令: `python3 -m unittest discover -s tests`，结果 81 passed。
- 下一步: 收敛与工程化，补 README、console script、清理工作区噪音和可选 lint/type checks。

### 2026-06-06 22:40:55 CST

- 新增 `README.md`，记录当前能力范围、CLI 快速开始和 MVP 缺口。
- `pyproject.toml` 增加 console script: `agent-kernel = agent_kernel.hosts.cli:main`。
- 验证命令: `python3 -m compileall -q agent_kernel tests`，通过。
- 验证命令: `python3 -m unittest discover -s tests`，结果 81 passed。
- 尚未清理 `.DS_Store` / `__pycache__`，避免未经明确授权删除文件。

### 2026-06-06 22:49:05 CST

- 对照 `python-code-architecture-design.md`、`TODO.md` 和当前代码完成复核。
- 确认当前实现是可运行 MVP 基座，不等价于完整架构目标。
- 新增“架构对照复核”章节，记录已正确落地的 MVP 基座、设计缺口和下一阶段优先级。
- 验证命令: `python3 -m compileall -q agent_kernel tests`，通过。
- 验证命令: `python3 -m unittest discover -s tests`，结果 81 passed。
- 未删除 `.DS_Store` / `__pycache__`。

### 2026-06-06 22:58:30 CST

- 新增 `LLMConfig.from_env()`，支持 `AGENT_KERNEL_LLM_*` 和常见 `OPENAI_*` 环境变量。
- 新增 `OpenAICompatibleProvider`，使用标准库调用 `/chat/completions`，避免引入 SDK 依赖。
- 新增 CLI `llm-smoke`，可在配置真实模型后发起一次真实 LLM 验证调用。
- 新增 README 真实 LLM smoke 配置说明。
- 新增测试覆盖 LLM 配置读取、OpenAI-compatible request payload 和 CLI smoke 调用路径。

### 2026-06-06 23:06:10 CST

- `LLMConfig` 增加 TOML/JSON 配置文件读取，CLI 支持全局 `--config`。
- 新增 `agent-kernel.example.toml`，推荐使用 `api_key_env` 避免明文密钥落盘。
- README 增加配置文件方式的真实 LLM smoke 示例。
- 新增测试覆盖 TOML、JSON 和 CLI `--config` 读取路径。

### 2026-06-06 23:56:30 CST

- 使用用户配置的 `agent-kernel.toml` 完成真实 LLM smoke 验证: provider=`openai-compatible`, model=`deepseek-v4-pro`。
- 验证 sample workflow CLI: `sample-run` 完成，`inspect` 可查询 timeline，`replay` 可复现完成状态且不重放外部调用。
- 分组验证 Capability/Approval/ToolCall、Memory/Context、Autonomy/Interaction/Agent Orchestration、Extension/Replay 代表性测试，全部通过。
- 真实 AgentLoop 首次验证发现 provider 返回 `content` 字符串而 AgentLoop 期望结构化 dict，已补充 JSON content 归一化。
- 真实 AgentLoop 二次验证通过: 模型返回 JSON 后 session 进入 `completed`，并持久化 `AgentTaskResult`。

### 2026-06-07 00:12:40 CST

- 新增 `scripts/verify_realized_todo.py`，按 Phase 0-9 构造临时真实场景，验证 TODO 中已勾选能力。
- 本地验收命令通过: `python3 scripts/verify_realized_todo.py --no-real-llm`。
- 真实 LLM 验收命令通过: `python3 scripts/verify_realized_todo.py --config agent-kernel.toml`。
- 验收覆盖: domain serialization/state、durable runtime/checkpoint/retry/dead-letter/budget/circuit、agent/supervisor/real AgentLoop、capability/policy/approval/process/audit、memory/context ledger、trace/cost/replay/eval/artifact、extension permission、autonomy workflow template/skill evolution、interaction fabric/taskboard/observer、CLI sample/inspect/replay/llm-smoke。
- 验收排除未勾选能力: 通用 Workbench 真实 adapter、HTTP 写操作 endpoint、SSE/WebSocket event stream、产品级 Codex/Claude/Gemini CLI shim、动态工具/工作流组合。
- 验证命令: `python3 -m compileall -q agent_kernel tests scripts`，通过。
- 验证命令: `python3 -m unittest discover -s tests`，结果 89 passed。

### 2026-06-07 00:55:20 CST

- 补 HumanInterventionService，支持追加 `human.intervention` 事件、写入 working memory，并在 `pause_and_resume` 模式下中断 run。
- CLI 新增 `intervene` 命令。
- 补 SkillService，支持创建 interpreted skill、compiled workflow skill、activate、list/select，并可将 compiled workflow 注册到 `WorkflowLibrary`。
- 补 PlanPatchValidator，校验 patch commands、goto target 和 required capabilities。
- 补 MCP adapter protocol + FakeMCPClient + stdio JSON-RPC client + MCP tool executor。
- 补 Workbench protocol + FakeWorkbenchClient。
- 补通用 `HTTPWorkbenchClient` + `HTTPWorkbenchEndpoint`，支持 JSON HTTP Workbench command envelope。
- `CapabilityRuntime` 支持 generic `workbench_client`，非 control workbench command 同样经过 policy、audit 和 tool_call 状态记录。
- 补 AgentConnector protocol + FakeAgentConnector，为后续真实 Codex/Claude/Gemini CLI connector 留接口。

### 2026-06-07 01:22:00 CST

- 对标 GenericAgent 的 `TMWebDriver`、`computer_use`、ADB/UI 控制思路，新增接口化 `ControlWorkbench`。
- 覆盖 browser `inspect/execute_js/navigate`、desktop `screenshot/click/key/type_text/dump_ui`、mobile `screenshot/dump_ui/tap/type_text` 等原子控制入口。
- `CapabilityRuntime` 支持 `kind="workbench"`，control 调用统一经过 policy、approval、audit 和 tool_call 状态记录。
- 新增 deterministic `FakeControlBackend`，用于真实 adapter 尚未接入前的可运行测试和 dry-run。
- 新增 `TMWebDriverHTTPBackend`，兼容 GenericAgent `/link` API，支持 `get_all_sessions`、`execute_js` 和基于 JS 的 `navigate`。
- 新增 `ADBMobileBackend`，兼容 GenericAgent `adb_ui.py` 的能力边界，支持设备枚举、UI dump 解析、tap、text、keyevent 和 screenshot。
- 新增 `Win32DesktopBackend`，通过可选 desktop driver 支持窗口枚举、截图、物理坐标 click、快捷键和剪贴板粘贴输入；公共 API 不照搬个人命名。
- 新增 `DesktopUIDetector` 协议和 `UIAStyleDesktopDetector`，将 UIA-like 控件树归一化为 control nodes 并接入 desktop `dump_ui`。
- 后续真实平台 adapter 可继续接入具体 UIA provider 和视觉检测，不需要修改 runtime/policy/agent 层。
- 新增 `AgentConnectorRouter`、`ConnectorRoute`、`RoutedConnectorTurn`，支持不同 participant 路由到不同 persistent connector/session，并把外部 session turn 写回 interaction channel。
- 新增 `StructuredStdioAgentConnector` 和 `StdioAgentCommand`，使用 JSONL `start/message/turn/stop` 帧连接长驻 CLI shim，不依赖终端文本 marker 判断完成。
- 新增 `ProductCLIConnectorSpec` 和 `ProductCLIConnectorFactory`，通过产品 shim 配置构建 connectors，并用多产品 JSONL shim 验证 routing。
- 新增 `DecisionArtifactService`、`DiscussionSummarizer` 和 deterministic summarizer，将 channel 消息归纳为 decision artifact，并回写 `GroupChatSession.decision_artifact_ref`。
- `DecisionArtifactService` 支持跨 channel 聚合，将多个 channel summary 归纳为 cross-channel decision artifact。
- 新增 `SpeakerSelector` 协议和 round-robin/free-for-all/moderator-select selector，`GroupChatService` 通过 selector 记录 `selected_by` 和 rationale。
- 补 host DTO: EventStreamEnvelope、TaskWorkspaceDTO、HTTPRouteSpec/default routes。
- 补 HTTP host 写控制接口: `POST /runs/{run_id}/cancel`、`POST /runs/{run_id}/interventions`、`POST /approvals/{approval_id}/approve|reject`、`POST /tool-calls/{tool_call_id}/cancel|kill`。
- 补测试覆盖 intervene、skill service、plan patch、MCP stdio/fake、Workbench fake、connector、host DTO。
- 当前浏览器控制已有 TMWebDriver HTTP adapter，移动控制已有 ADB adapter，桌面控制已有 Win32 desktop adapter 和 UIA-style tree detector；具体 UIA provider 与视觉检测仍待补。

## 风险与待决策

- [ ] Python 版本暂按 `>=3.11` 设计；如需兼容 3.10 需要调整类型语法。
- [ ] 数据校验库优先使用 Pydantic v2；如要求零依赖，需要改成 dataclasses + 手写校验。
- [ ] SQLite 为 MVP 默认存储；后续如要多进程高并发，需要评估 Postgres。
- [x] CLI AgentConnector 的终端完成判定不能依赖纯文本 marker，需要结构化事件或 adapter 层协议。
- [x] 多 coding agent 修改同一仓库必须实现 worktree isolation 或 patch review，否则容易互相覆盖。
