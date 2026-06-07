# Agent 架构调研分析报告 v2

更新时间，2026-06-03，UTC，
调研范围，`SpectrAI`、`desktop-cc-gui`、`AionUi`、`codeg`、`GenericAgent`、`ClaudeCodeRev`、`LangGraph`、`LangChain`、`CrewAI`、`AutoGen`

---

## 变更摘要

本报告 v2 在 v1 基础上补齐了若干关键架构结论，并统一收敛到本节说明，正文不再重复标注“v2 新增”。

1. **“一切都是 Node”递归组合原则**，将 Agent、Workflow、Tool、Human、Memory 操作统一纳入节点抽象，并落到工作流接口与模块设计中，§3.6.1、§3.6.3、§4.3.2、§4.4.2，。
2. **4 种协作模式的系统分类**，将子代理协作扩展为 Supervisor-Worker、Group Chat、Pipeline、Blackboard / Task Board 四类，§3.4，。
3. **能力地图方法论**，引入 10 大能力域，并据此重构项目分析模板与横向对比矩阵，§1.0、§2、§3.6.2，。
4. **Memory 与 Context 分离**，明确把长期存储层与运行时输入层拆成不同模块，并在记忆系统与上下文管理器之间通过 Retrieval Pack 对接，§4.3.5、§4.3.6、§4.8.6、§4.8.7，。
5. **Agent，CLI，Workflow 三类 Adapter**，把外部能力统一包装为一等可调用能力，避免各类集成各自发明接口，§4.9.5，。
6. **98.4% harness vs 1.6% intelligence 洞察**，将实现优先级从“先做聪明”调整为“先做稳定 runtime、状态、事件与恢复”，§3.7、§4.12，。
7. **Event Sourcing 与结构化 IO**，补强事件溯源、可回放性、结构化输入输出契约和审计链设计，§4.3.1、§4.3.7、§4.4.7，。

---

## 0. 调研方法与判断口径

本报告不是只看 README 的产品说明，而是同时结合了以下信息源，

- 各仓库 README / docs / design 文档
- 顶层目录结构与关键源码目录
- 运行时、工具、记忆、工作流、多代理相关核心文件
- GitHub 仓库元数据，star、fork、issue、最近推送时间、license

判断时采用两个口径，

1. **框架口径**，这个项目是否提供可复用的抽象、运行时、编排层、扩展点。
2. **产品口径**，这个项目是否主要是面向终端用户的桌面、工作台、宿主应用，而非可复用框架。

这一区分很重要，因为本次目标是设计一个“高度抽象、模块化、可替换”的 Agent 框架。很多项目值得参考，但不适合作为内核基座。

---

## 1. 各项目详细分析

### 1.0 统一分析模板，10 维能力画像 + 证据说明

从本版开始，各项目统一按能力地图的 10 个维度做首轮画像，再用后续叙述补充实现证据。这样可以避免“功能清单很多，但缺乏统一评估标尺”的问题。

| 维度 | 关注问题 |
|---|---|
| 定位与核心抽象 | 这是框架、宿主应用、GUI、CLI，还是工作流引擎，核心对象是 Agent、Graph、Task、Tool 还是 Session， |
| 推理能力 | 是否体现 planning、reflection、critique、verification 等推理机制， |
| 执行能力 | 是否能稳定执行工具、CLI、文件操作、代码执行、浏览器操作， |
| 编排能力 | 是否支持 DAG、循环、条件路由、并行、子工作流、重试、人类介入， |
| 协作能力 | 是否支持子代理、群聊、主管-工人、任务板、委派协议， |
| 记忆能力 | 是否具备 working、episodic、semantic、procedural、artifact 等分层记忆， |
| 上下文能力 | 是否有检索、压缩、摘要、优先级排序、token 预算控制， |
| 运行时能力 | 是否有事件日志、checkpoint、恢复、取消、流式输出、trace， |
| 安全与治理 | 是否有权限、沙箱、审批、脱敏、审计链， |
| 集成与产品化 | 是否能接入 MCP、API、CLI、数据库、UI、工作区、历史记录、可视化， |

阅读方式建议如下，

1. 先看每个项目的“定位与核心功能”“架构设计模式”，判断它是框架样本还是产品样本。
2. 再用上面的 10 维模板映射到“工作流、工具、记忆、子代理、扩展机制”等证据段落。
3. 最后结合 §2 的横向矩阵，判断它更适合作为内核蓝本、子系统蓝本，还是仅作局部灵感来源。

### 1.1 SpectrAI

仓库:<https://github.com/wei9966/SpectrAI>
截至 2026-06-03，91 stars，22 forks，4 open issues，最近推送 2026-04-04。

#### 项目定位与核心功能

SpectrAI 本质上是一个 **Electron 多 AI CLI 会话编排桌面客户端**。它强调，

- 多 Provider 会话统一管理
- 结构化对话视图
- 会话级 Agent 编排
- 文件变更追踪
- Git worktree 隔离
- MCP 工具桥接

从 README 看，它试图覆盖 "多会话 + 子 Agent + Teams + 工作流 + 看板 + Telegram 远控"。但从当前代码快照看，**已稳定落地的是 Provider 适配、会话管理、子 Agent 管理、SQLite 持久化、输出解析和文件追踪**。

#### 架构设计模式

代码中的主干非常明确，

- `src/main/adapter/*`，Provider Adapter 层
- `src/main/session/SessionManagerV2.ts`，事件驱动的会话协调层
- `src/main/agent/AgentManagerV2.ts`，子代理编排层
- `src/main/agent/AgentMCPServer.ts`，MCP stdio/server bridge
- `src/main/parser/*`，CLI 输出解析与状态推断
- `src/main/storage/*`，SQLite 存储
- `src/main/task/TaskSessionCoordinator.ts`，任务-会话联动

核心设计模式有三个，

- **Adapter 模式**，`AdapterRegistry.ts` + `BaseProviderAdapter` 抽象，把 Claude/Codex/Gemini/iFlow/OpenCode 的协议差异收敛到统一接口。
- **Thin Coordinator 模式**，`SessionManagerV2` 和 `AgentManagerV2` 尽量只做状态协调与事件路由，真正的会话行为下沉到 adapter。
- **Bridge + MCP 注入模式**，`AgentMCPServer.ts` 以独立进程提供 stdio MCP Server，再通过 WebSocket 连接本地 `AgentBridge`。

这是一个比较典型的"宿主应用 runtime"架构，而不是纯框架。

#### Agent 模型

当前源码明确支持的是 **层级式 Supervisor -> Child Agent**，

- `spawnAgent`
- `sendToAgent`
- `waitAgent`
- `waitAgentIdle`
- `cancelAgent`

子 Agent 由 `AgentManagerV2.ts` 创建为独立子会话，并保留，

- `parentSessionId`
- `childSessionId`
- `providerId`
- `oneShot / persistent`

这意味着它的代理模型是，

- 默认单中心调度
- 子代理是父代理的"工具化执行单元"
- 支持一次性任务和持久代理两种形态

README 里宣称还有 `Agent Teams` 去中心化协作，但当前 `src/main/team/`、`src/main/orchestrator/`、`src/main/planner/` 等目录在当前快照中并不存在，说明 **文档前置于代码实现**。

#### 工作流引擎

README 和 `schema.sql` 中存在 `workflows` 与 `workflow_executions` 表，说明作者已经为工作流做了数据模型预留;但当前源码里没有真正的 `Orchestrator.ts` 或 DAG 引擎实现。

当前真正落地的"工作流"能力主要是，

- 会话状态驱动任务状态
- 子 Agent 等待/回收
- worktree 隔离

所以 SpectrAI 当前更接近，

- **会话/任务编排器**
- 不是完整的通用工作流引擎

#### 工具调用机制

这是它最值得参考的部分之一。

`AgentMCPServer.ts` 展示了几层关键机制，

- 会话模式控制工具可见性，`supervisor / awareness / member`
- MCP 工具通过 WebSocket 发到 `AgentBridge`
- 文件编辑类工具本地执行，并计算 operation diff 与 cumulative diff
- 用 `toolMapping.ts` 把不同 Provider 的工具事件统一成内部事件模型

优点，

- 把"Provider 协议"与"内部工具语义"分开
- 允许不同 CLI 的事件结构被统一消费
- 工具调用结果不仅是文本，还伴随变更 diff、状态事件

#### 记忆与上下文管理

这部分相对薄，

- `sessions`、`session_logs`、`activity_events`
- `session_logs_fts` 全文检索
- `ConversationMessage` 结构化存储

但它没有像 CrewAI/GenericAgent 那样明确的，

- 分层长期记忆
- 语义记忆检索
- episodic / semantic / procedural memory 区分

因此它的"上下文"仍主要依赖，

- 底层 Provider 会话历史
- 本地结构化消息持久化
- MCP 注入与跨会话感知

#### 子代理机制

`AgentManagerV2.ts` 做得相当实用，

- 子会话通过 `createSessionWithId` 直接创建
- 针对不同 Provider 动态生成 MCP 配置
- `turn_complete` 事件作为 deterministic readiness signal
- `wait_agent_idle` 用 idle flag 规避竞态

这里的经验非常重要，**不要靠终端文本 marker 猜"代理是否完成"，而应靠结构化事件判断**。

#### 扩展机制

主要扩展点，

- Provider Adapter 注册
- 自定义 Provider
- MCP 配置注入
- skill IPC / builtin skills

但目前不算强插件系统，没有像 AionUi 那样完整 manifest 驱动扩展模型。

#### 优缺点分析

优点，

- Provider 抽象做得清楚
- 子代理生命周期管理较务实
- 使用 `turn_complete` 做就绪判断，比 PTY 推断可靠
- 工具调用和文件 diff 结合得很好

缺点，

- README 中的团队、工作流、规划、Telegram 等模块，与当前代码快照存在明显落差
- 记忆系统弱
- 架构仍偏 Electron 产品宿主，不是框架内核

结论，

- **适合作为"多 Provider 适配层 + 子代理生命周期管理"参考**
- **不适合作为完整 Agent 框架基座**

---

### 1.2 desktop-cc-gui

仓库:<https://github.com/zhukunpenglinyutong/desktop-cc-gui>
截至 2026-06-03，2752 stars，240 forks，177 open issues，最近推送 2026-06-01。

#### 项目定位与核心功能

desktop-cc-gui 是一个 **本地优先的 AI 编程工作台**，核心不是抽象 agent runtime，而是，

- 多 coding engine 统一宿主
- 会话观察与治理
- 项目记忆
- 上下文账本
- Git / worktree / terminal / 任务中心

它明显是"开发者工作台产品"，不是框架。

#### 架构设计模式

前端是大量 feature slice，

- `src/features/context-ledger`
- `src/features/project-memory`
- `src/features/parallel`
- `src/features/collaboration`
- `src/features/tasks`

后端 Tauri Rust 侧也分得很细，

- `src-tauri/src/engine/*`
- `src-tauri/src/runtime/*`
- `src-tauri/src/project_memory/*`

几个值得参考的模式，

- **Feature-sliced UI + backend service split**
- **运行时台账runtime ledger**，`runtime/ledger.rs`
- **孤儿进程清理 + 恢复**，`session_lifecycle.rs`
- **项目记忆投影与注入**，前后端分别有 projection / retrieval 逻辑
- **Spec-first 工程治理**，`.trellis/spec/**` 与 `openspec/**`

#### Agent 模型

从产品能力看，它支持多 engine 并行;但从源码抽象看，核心更像 **workspace session runtime**，不是通用 agent graph。

`engine/manager.rs` 管理的是，

- Claude session
- OpenCode session
- Gemini session
- Codex 运行时兼容面

代理协作更多体现为，

- 会话间并列
- workspace 级投影
- 历史/上下文治理

而不是统一的子代理通信协议。

#### 工作流引擎

它有 planning/task center，但不是 DAG 编排框架。

更准确地说，它的 workflow 是，

- UI 层的执行投影
- 运行时记录与治理界面
- Trellis/OpenSpec 驱动的开发流程

所以它在"产品内流程治理"上很强，在"框架级工作流编排"上不强。

#### 工具调用机制

它通过 Tauri service bridge 暴露工具化后端能力，

- `src/services/tauri/sessionManagement.ts`
- `src/services/tauri/projectMemory.ts`
- `src/services/tauri/terminalRuntime.ts`

特点是，

- 工具能力不是以 LLM function-call schema 为核心，而是以桌面应用服务能力为核心
- 更偏"GUI 宿主 API"

#### 记忆与上下文管理

这是它最值得借鉴的部分之一。

前端，

- `memoryContextInjection.ts`，基于 query term / relevance / importance 的注入策略
- `projectMemoryRetrievalPack.ts`
- `memoryKindClassifier.ts`

后端，

- `src-tauri/src/project_memory/store.rs`
- `projection.rs`
- `classification.rs`

再配合，

- `context-ledger` 做上下文构成、成本、治理可视化

这是一种"**产品级上下文治理架构**"，比单纯 memory store 更接近生产环境需求。

#### 子代理机制

产品层面能展示并行协作和子会话，但在当前代码里没有一个像 `spawn_agent` / `mailbox` / `taskboard` 那样清晰的统一子代理协议。

`.trellis/scripts/multi_agent/*` 倒是提供了开发流程用的多代理编排脚本，但这是项目自用开发工具链，不是产品内核。

#### 扩展机制

扩展主要是，

- `.agents/skills/*`
- `.claude/**`
- `.codex/**`
- OpenSpec / Trellis

它更像"**面向 AI 开发者协作的仓库治理框架**"，不是运行时插件机制。

#### 优缺点分析

优点，

- 运行时治理、可恢复性、可观测性很强
- 项目记忆 + 上下文账本设计成熟
- 前后端都做了明确 feature 切分
- 工程规范和可维护性明显高于多数业余项目

缺点，

- 通用 Agent 抽象较弱
- 工作流与多代理能力主要服务产品，不是通用框架
- 插件能力不如 AionUi 清晰

结论，

- **适合作为"产品外壳、可观测性、上下文治理、项目记忆系统"参考**
- **不适合作为 Agent 内核框架直接复用**

---

### 1.3 AionUi

仓库:<https://github.com/iOfficeAI/AionUi>
截至 2026-06-03，27462 stars，2645 forks，599 open issues，最近推送 2026-06-02。

#### 项目定位与核心功能

AionUi 的定位是 **Cowork 应用**，既能宿主内建 agent，也能统一接入 Claude Code、Codex、Qwen Code、OpenClaw 等多种 CLI Agent，并提供，

- Team Mode
- WebUI
- 多端远程访问
- 扩展 SDK
- assistants / skills / MCP 管理

#### 架构设计模式

从源码看，AionUi 的关键特点不是"把全部 agent runtime 写在前端仓库里"，而是 **宿主层与 backend 二进制分离**，

- `packages/web-host/src/backend-launcher.ts`
- `packages/web-host/src/agent-process-registry.ts`
- `packages/desktop/src/process/*`

`@aionui/web-host` 的 README 直接写明职责，

- backend-launcher，启动或复用 `aioncore`
- static-server，提供 WebUI + 反向代理
- auth，认证与密码管理

也就是说，这个仓库能看到的是 **Electron / WebUI 宿主、桥接和扩展面**;真正的核心 agent backend 逻辑，大量在 `aioncore` 侧，而不完全在当前 TypeScript 仓库中。

#### Agent 模型

README 描述的模型很强，

- built-in agent
- 多 CLI agent 并行
- Team Mode，Leader + Teammates

在代码层面，当前仓库可见的证据主要是，

- `schema.ts` 中的 `teams`、`mailbox`、`team_tasks`
- agent process registry
- extension manifests 对 agents / assistants / skills 的贡献点

因此可以下结论，

- **产品级多代理是有的**
- 但 **可直接审计的 runtime 细节并不都在本仓库**

#### 工作流引擎

README 提到 cron、24/7 自动化、Office assistants、remote control，但当前 TypeScript 侧并未暴露出像 LangGraph / CrewAI Flow 那样的通用 DSL 或图执行器。

当前可见的是，

- host/backend lifecycle
- web host
- DB schema
- extension integration

所以它的工作流抽象更多沉在 backend，不适合把当前仓库当成完整 workflow engine 参考。

#### 工具调用机制

这部分很强，尤其是生态设计。

扩展 manifest `aion-extension.json` 支持，

- `acpAdapters`
- `mcpServers`
- `assistants`
- `agents`
- `skills`
- `themes`
- `settingsTabs`

并且声明权限，

- storage
- network
- shell
- filesystem
- events

这是一个比较成熟的 **manifest-driven extension model**。

它的价值不在"单个工具如何调"，而在"第三方能力如何声明式接入整个宿主平台"。

#### 记忆与上下文管理

当前仓库可见的主要是，

- conversations / messages
- assistant rules
- skills
- 远程 channel/session 状态

没有看到像 CrewAI unified memory、GenericAgent layered memory 那样完整的认知记忆模型。它更偏，

- 会话持久化
- assistant preset / skill loading
- backend owned rule storage

#### 子代理机制

从产品描述看 Team Mode 明确存在，DB 里也有，

- `teams`
- `mailbox`
- `team_tasks`

这是一个典型的，

- 共享任务板
- 邮箱式代理通信

但当前仓库没有把 Team Orchestrator 的完整实现暴露出来，说明关键逻辑在 backend。

#### 扩展机制

这是 AionUi 最强的一项。

优势，

- 清晰 manifest
- 贡献点多
- 权限模型明确
- 主题、skills、assistants、agents、MCP servers 都可注入

对于新架构设计，这是非常值得借鉴的。

#### 优缺点分析

优点，

- 插件/扩展模型很完整
- WebUI host 与 Electron host 分层合理
- 多 agent / channel / remote access 产品化能力强

缺点，

- 核心 runtime 大量在外部 backend，当前仓库不够透明
- 框架抽象不如 LangGraph / AutoGen / CrewAI 清楚
- 记忆系统在当前源码层不突出

结论，

- **适合作为"宿主平台 + 扩展 SDK + manifest 权限系统"参考**
- **不适合作为新框架内核的唯一蓝本**

---

### 1.4 codeg

仓库:<https://github.com/xintaofei/codeg>
截至 2026-06-03，1472 stars，160 forks，83 open issues，最近推送 2026-06-02。

#### 项目定位与核心功能

codeg 是一个 **多代理 coding workspace**，强调，

- 聚合同类 agent 会话
- 在一个主会话里把任务委派给不同 agent
- desktop / server / docker 三种部署形态
- 远程 workspace
- chat channels

#### 架构设计模式

其架构值得注意的点，

- 前端，Next.js 工作台
- 后端，Rust + Tauri + SeaORM
- delegation companion，`codeg-mcp`

最核心的是 `src-tauri/src/acp/delegation/*`，

- `broker.rs`
- `spawner.rs`
- `listener.rs`
- `transport.rs`
- `tool_schema.json`

这是一个非常明确的"**子代理委派运行时**"。

#### Agent 模型

codeg 的多代理不是"大家都在群聊里说话"，而是，

- 父会话继续主线工作
- 子代理拿到完整独立任务后后台运行
- 通过 `task_id` 异步回收

这是典型的 **异步父子任务树**。

和 SpectrAI 的差别是，

- SpectrAI 更偏"子会话 + wait/idle"
- codeg 更偏"异步 delegation task runtime"

#### 工作流引擎

它没有 LangGraph 那样显式 DAG，也没有 CrewAI Flow 那样 DSL。

但它实现了一个很重要的实战模式，

- **异步可追踪的 delegation workflow**

`delegate_to_agent -> get_delegation_status -> cancel_delegation`

这是一种围绕子任务的 workflow primitive，而不是围绕节点图的 workflow engine。

#### 工具调用机制

`tool_schema.json` 很清楚，

- `delegate_to_agent`
- `get_delegation_status`
- `cancel_delegation`

`broker.rs` 则负责，

- 深度限制
- spawn child
- 发送首条 prompt
- 结果缓存
- 取消级联
- 父任务 teardown

`spawner.rs` 把真正的连接管理抽成 trait，

- 便于 mock 测试
- 便于未来接 remote-agent backend

这是很好的抽象。

#### 记忆与上下文管理

codeg 的记忆更多是，

- conversation database
- imported sessions
- skills at global/project scope

它没有独立的语义记忆内核。上下文主要来自，

- 当前 conversation
- 子 session lineage
- workspace 文件环境

#### 子代理机制

这恰恰是 codeg 的最强项。

关键实现，

- `conversation` 表增加 `parent_id / parent_tool_use_id / delegation_call_id`
- `DelegationBroker` 维护 running/completed task
- 子任务完成后迁移状态并可回收结果
- 取消支持 parent cancel / child cancel / tool cancel 多路径

这是一个很成熟的 **subagent lineage + task lifecycle** 设计。

#### 扩展机制

扩展主要有，

- MCP companion
- chat channels
- remote workspace
- server mode

但没有像 AionUi 那样完整统一的 extension SDK。

#### 优缺点分析

优点，

- 子代理委派模型明确且可测试
- Rust 抽象质量不错
- 支持 server / browser / docker，部署弹性好

缺点，

- 记忆体系弱
- 工作流不是通用图引擎
- 更像"coding workspace runtime"，不是通用 agent framework

结论，

- **适合作为"异步子代理任务运行时"核心参考**
- **尤其适合借鉴 task lineage、status polling、cancel cascade**

---

### 1.5 GenericAgent

仓库:<https://github.com/lsdefine/GenericAgent>
截至 2026-06-03，12417 stars，1433 forks，119 open issues，最近推送 2026-06-02。

#### 项目定位与核心功能

GenericAgent 的定位非常鲜明，**极简、自进化、自成长的自治 agent**。

它的主张是，

- 核心代码量非常小
- 工具只保留原子能力
- 行为增长主要来自 memory / SOP / skill 沉淀

#### 架构设计模式

最核心的几个文件，

- `agent_loop.py`
- `agentmain.py`
- `ga.py`
- `assets/tools_schema.json`
- `memory/memory_management_sop.md`

架构上不是典型 OO 分层，而是，

- **最小循环**
- **工具 schema**
- **Handler dispatch**
- **memory 驱动行为演化**

`agent_runner_loop` 的逻辑几乎就是整个大脑主循环，

- 组装 system + user
- 调 LLM
- 解析 tool calls
- `handler.dispatch`
- 根据 `next_prompt` 继续循环

这是一种极端的 **kernel + prompt/memory externalization**。

#### Agent 模型

默认是单 agent 核心。

但通过，

- `memory/subagent.md`
- `memory/goal_hive_sop.md`
- `memory/incubator_sop.md`

它扩展出了，

- subagent
- master/worker
- agent network

问题在于，这些机制大多是 **SOP 驱动的隐式协议**，不是强类型 runtime API。

#### 工作流引擎

GenericAgent 几乎没有显式 workflow engine。

它的工作流存在于，

- SOP 文本
- long-term memory
- slash commands
- tool loop

例如，

- `plan_sop`
- `review_sop`
- `autonomous_operation_sop`
- `morphling_sop`

优点是极灵活;缺点是难做静态分析、可视化和恢复。

#### 工具调用机制

`assets/tools_schema.json` 定义了 9 个原子工具，

- `code_run`
- `file_read`
- `file_patch`
- `file_write`
- `web_scan`
- `web_execute_js`
- `update_working_checkpoint`
- `ask_user`
- `start_long_term_update`

这套设计体现出两个关键思想，

1. **工具越原子越稳定**
2. **记忆本身也是工具**

#### 记忆与上下文管理

这是 GenericAgent 的核心竞争力。

它不是单一 memory，而是明确分层，

- L0，Meta Rules
- L1，Insight Index
- L2，Global Facts
- L3，Task Skills / SOPs
- L4，Session Archive

配套机制，

- `update_working_checkpoint`，短期工作记事板
- `start_long_term_update`，长期记忆提炼
- `global_mem_insight.txt`，极简索引
- `memory/L4_raw_sessions/*`，历史会话压缩与挖掘

这是一种非常值得借鉴的 **working memory / semantic memory / procedural memory / episodic memory 分层**。

#### 子代理机制

能力有，但实现不是强 runtime，而是 SOP 和多 session 协调。

优点，

- 灵活
- 几乎不需要额外基础设施

缺点，

- 协议是隐式的
- 可恢复性、可观测性、权限隔离都弱

#### 扩展机制

扩展主要通过，

- `plugins/hooks`
- `memory/skill_search`
- `skills/` / `memory/*.md`

本质是 **技能与记忆共生式扩展**，而不是 manifest 插件。

#### 优缺点分析

优点，

- 极简内核
- 分层记忆设计优秀
- 自进化思想鲜明

缺点，

- 大量行为外置到 prompt/SOP，调试和治理难
- 子代理、多工作流、权限体系不够显式
- 工程可维护性依赖作者强约束

结论，

- **适合作为"记忆系统"和"极简内核设计"参考**
- **不适合作为企业级多代理 runtime 的直接蓝本**

---

### 1.6 ClaudeCodeRev

仓库:<https://github.com/Haleclipse/ClaudeCodeRev>
截至 2026-06-03，68 stars，56 forks，0 open issues，最近推送 2026-04-02。

#### 项目定位与核心功能

这是一个 **从已发布 npm 包 sourcemap 反编译恢复出来的 Claude Code 2.1.88 源码树**。它的价值不在 license/可用性，而在于可以观察到一个一线 agent coding CLI 的内部设计。

#### 架构设计模式

几个关键核心，

- `src/QueryEngine.ts`，对话主引擎
- `src/Tool.ts`，工具、权限、上下文总线类型
- `src/tools/AgentTool/AgentTool.tsx`，子代理工具
- `src/services/SessionMemory/sessionMemory.ts`，会话记忆
- `src/tasks/*`，后台任务

总体是典型的，

- **消息流驱动**
- **工具上下文巨对象**
- **插件/技能/MCP 并列接入**
- **子代理、工作流、任务统一进入 task 子系统**

#### Agent 模型

可见的模型非常强，

- main agent
- background agent
- teammate
- worktree-isolated agent
- remote agent

`AgentTool.tsx` 暴露出的能力包括，

- 指定子代理类型
- 背景运行
- worktree / remote 隔离
- named subagent
- mode / cwd 覆盖

这说明它不是简单的工具调用，而是 **子代理即一等公民**。

#### 工作流引擎

项目里有 `commands/workflows`、`local_workflow` task 类型等痕迹，但当前快照中 `LocalWorkflowTask` 只是生成 stub。

这说明，

- 原始产品里应存在 workflow 能力
- 当前恢复源码并不完整

因此只能把它当成"先进产品设计样本"，不能当稳定框架分析。

#### 工具调用机制

`Tool.ts` 非常有参考价值，

- Tool schema
- permission context
- tool progress
- MCP server connection
- agent definition
- tool JSX/UI

它把工具系统设计成了一个非常厚的运行时层，而不是简单 JSON function 调用。

#### 记忆与上下文管理

`SessionMemory/sessionMemory.ts` 显示，

- 定期后台 fork subagent
- 自动维护 markdown session memory
- 按 token 阈值和 tool-call 阈值触发

这是一种很实用的 **后台异步 session summarization**。

此外还有，

- CLAUDE.md 注入
- nested memory attachment
- dynamic skill discovery

#### 子代理机制

这是它最强的部分之一。

`AgentTool.tsx` 已经把子代理当成，

- 工具
- 任务
- 会话
- UI 对象

四者同时存在的实体来处理。

这对新架构很有启发，**subagent 不应只存在于 LLM prompt 里，也不应只存在于数据库里，而应有统一 runtime entity。**

#### 扩展机制

明显有，

- plugins
- skills
- MCP
- commands
- remote / proactive / coordinator mode

但由于代码是恢复版，并且带有大量 feature-gated stub，不能假设这些模块都完整可运行。

#### 优缺点分析

优点，

- 实战型产品设计非常先进
- 工具、权限、子代理、记忆都做得深
- 非常适合提炼真实世界 coding agent 的运行时需求

缺点，

- 法律与合规风险
- 恢复源码存在 stub 和缺口
- 不适合作为直接依赖的开源基座

结论，

- **适合作为"高级产品能力样本"参考**
- **不适合作为新框架基础仓库**

---

### 1.7 LangGraph

仓库:<https://github.com/langchain-ai/langgraph>
截至 2026-06-03，33699 stars，5672 forks，562 open issues，最近推送 2026-06-02。

#### 项目定位与核心功能

LangGraph 是本次调研中最纯粹的 **低层状态化 Agent / Workflow 编排框架**。

它强调，

- durable execution
- interrupts / human-in-the-loop
- memory / checkpoint
- subgraph
- stateful multi-step workflow

#### 架构设计模式

核心结构极其清楚，

- `graph/state.py`，`StateGraph` 构建器
- `pregel/main.py`，运行时
- `channels/*`，状态聚合与通信通道
- `types.py`，`Command`、`Send`、`StreamMode`、checkpoint payload 等核心类型
- `checkpoint/*`，checkpointer 抽象与实现

它的关键模式是，

- **Builder + Compiled Runtime**
- **Shared State + Partial Update**
- **Reducer-based State Aggregation**
- **Pregel-style execution loop**

这是高度抽象、又非常可验证的架构。

#### Agent 模型

LangGraph 自身不预设"agent persona"，它提供的是，

- graph node
- state
- command
- send
- subgraph

因此它同时可表达，

- 单 agent 线性链
- DAG 工作流
- 带循环的 plan-act-reflect
- 多节点协作
- subgraph 嵌套

它在抽象层上比 AutoGen 更中性，比 CrewAI 更底层。

#### 工作流引擎

这是 LangGraph 的最强项。

`StateGraph` 可表达，

- 线性
- 条件分支
- loop
- waiting edges
- subgraph
- interrupt before / after
- `Command` 动态路由
- `Send` 派发

如果新架构把工作流引擎视为一等公民，LangGraph 是最值得学习的对象。

#### 工具调用机制

LangGraph 核心没有把工具系统写死在 graph 内核，而是通过，

- prebuilt `ToolNode`
- LangChain tool abstractions
- stream tool handlers

把工具系统作为上层组件接入。

这比"引擎内置某一套 tool schema"更可扩展。

#### 记忆与上下文管理

LangGraph 的记忆设计分两层，

1. **短期工作记忆**，state
2. **持久恢复记忆**，checkpoint / store

`InMemorySaver`、Postgres/SQLite saver 等实现说明，

- 它非常重视 durable execution
- 记忆首先服务"可恢复状态机"

相比 CrewAI/GenericAgent，LangGraph 不直接给你 semantic memory 体验，而是给你 **state persistence substrate**。

#### 子代理机制

LangGraph 没有像 codeg 那样显式 `spawn_agent` API，但有两个强能力，

- subgraph
- `Command` / `Send`

源码中还明确支持，

- 子图命名空间
- parent command
- subgraph stream transformer
- interrupt / resume 传播

这意味着它的子代理更像"**可组合子工作流/子状态机**"，而不是"新的独立会话进程"。

#### 扩展机制

扩展点很完整，

- checkpoint saver
- store
- prebuilt node
- SDK / JS bindings
- stream transformer

优点是抽象非常干净，缺点是对业务开发者不够开箱即用。

#### 优缺点分析

优点，

- 工作流抽象最强
- 状态机 / checkpoint / interrupt 设计成熟
- 内核与上层能力分离清楚

缺点，

- 使用门槛高
- 高级多代理协作需要自己建模
- 语义记忆、工具生态需要结合其他层

结论，

- **新框架工作流内核的首要参考对象**

---

### 1.8 LangChain

仓库:<https://github.com/langchain-ai/langchain>
截至 2026-06-03，138358 stars，22919 forks，605 open issues，最近推送 2026-06-02。

#### 项目定位与核心功能

LangChain 现在更像一个 **agent engineering platform**，

- `langchain_core` 提供模型/消息/工具/runnable 基础抽象
- `langchain_v1` 提供现代 agent factory
- `langchain_classic` 保留大量历史 chain/memory/tool 资产

#### 架构设计模式

现代部分最关键的文件是，

- `libs/langchain_v1/langchain/agents/factory.py`
- `libs/langchain_v1/langchain/agents/_subagent_transformer.py`
- `libs/core/langchain_core/*`

架构特点，

- **core 抽象层与 agent factory 分离**
- **middleware pipeline**
- **agent 构建在 LangGraph 之上**
- **经典模块兼容层长期共存**

#### Agent 模型

LangChain v1 的主模型是，

- 单 agent 为主
- nested named agents 可被 surface 成 subagent handle

`_subagent_transformer.py` 说明它已经意识到，

- 子代理是独立生命周期事件流
- 需要在 run stream 中显式暴露

但总体上，LangChain 仍然不是"team orchestration framework"，而是"高层 agent factory"。

#### 工作流引擎

LangChain 自己不是主要 workflow engine;它把复杂工作流能力委托给 LangGraph。

因此，

- 简单 agent，LangChain
- 复杂状态工作流，LangGraph

这也是它最大的架构优点之一，**分层清楚，不试图在一个层级解决所有问题。**

#### 工具调用机制

LangChain 的最大优势是工具生态，

- `langchain_core.tools`
- 海量 provider / tool integrations
- middleware 可在模型调用前后干预工具选择与执行

它不是最强 runtime，但几乎是最强生态入口。

#### 记忆与上下文管理

这里存在"新旧两套体系"，

- 新体系，借助 LangGraph state/checkpointer
- 旧体系，`langchain_classic/memory/*`

旧体系包括，

- buffer memory
- summary memory
- token buffer
- vectorstore memory
- entity / KG memory

问题是，

- 资产非常丰富
- 但同时也意味着概念负担和遗留复杂度较高

#### 子代理机制

v1 已经支持子代理可视化流，但仍不如 codeg/AutoGen/CrewAI 那样直接面向多代理协作。

#### 扩展机制

这是 LangChain 最强的一项，

- provider integrations
- tool ecosystem
- vector stores
- retrievers
- output parsers
- middleware

#### 优缺点分析

优点，

- 生态最大
- core 抽象稳
- 和 LangGraph 组合非常强

缺点，

- 新旧体系共存，复杂度高
- 多代理不是最强项
- 作为"新框架蓝本"容易把包袱一起带入

结论，

- **适合作为模型/工具/集成生态层参考**
- **不建议直接复制其完整包结构**

---

### 1.9 CrewAI

仓库:<https://github.com/crewaiinc/crewai>
截至 2026-06-03，52701 stars，7350 forks，389 open issues，最近推送 2026-06-03。

#### 项目定位与核心功能

CrewAI 是一个很明确的 **多代理自动化框架**，并把能力拆成两层，

- `Crews`，自治协作
- `Flows`，生产级事件驱动流程

这点非常关键，因为它没有把"agent autonomy"和"业务流程控制"混在一起。

#### 架构设计模式

关键模块，

- `crew.py`
- `flow/runtime.py`
- `agent/core.py`
- `memory/unified_memory.py`
- `memory/recall_flow.py`
- `tools/agent_tools/agent_tools.py`
- `events/*`

它的架构风格是，

- **高层对象模型Crew / Agent / Task / Flow**
- **事件总线**
- **统一记忆**
- **工具/知识/MCP/A2A 作为外围能力**

#### Agent 模型

CrewAI 的 agent 模型是本次调研里最"角色化"的之一，

- role
- goal
- backstory
- allow_delegation
- manager / hierarchical process

`Process` 当前枚举是，

- `sequential`
- `hierarchical`

说明它的多代理协作仍有明确中心化倾向，不是任意网状图。

#### 工作流引擎

Flow 体系是 CrewAI 的第二条主线。

`flow/runtime.py` + DSL decorators 支持，

- `@start`
- `@listen`
- `@router`
- persistence
- pause/resume
- human feedback

与 LangGraph 相比，

- 抽象更高，更偏应用开发
- 灵活性略弱，但 DX 更好

#### 工具调用机制

工具层包含，

- BaseTool
- `ToolsHandler`
- `AgentTools`delegate / ask coworker
- MCP resolver

这意味着 CrewAI 把"代理间互相委托"也工具化了，这点和 AutoGen AgentTool 思路相近。

#### 记忆与上下文管理

`Memory` 是 CrewAI 的亮点。

特点，

- unified memory
- LLM 分析后存储
- pluggable storage
- scope / slice 视图
- `EncodingFlow` 写入
- `RecallFlow` 检索

`RecallFlow` 甚至已经包含，

- query distillation
- time-based filtering
- parallel multi-scope search
- confidence-based routing
- iterative deepening

这是本次调研里最接近"**工程化语义记忆系统**"的实现之一。

#### 子代理机制

CrewAI 的子代理并不是独立 session runtime，而是，

- crew 内部角色之间的 delegation
- hierarchical manager 调度
- `DelegateWorkTool` / `AskQuestionTool`

这种模式适合业务协作，但隔离度不如 codeg/ClaudeCodeRev。

#### 扩展机制

CrewAI 现在的扩展面很大，

- hooks
- events
- skills
- knowledge sources
- MCP
- A2A
- tracing

#### 优缺点分析

优点，

- 多代理与流程分层清晰
- 统一记忆系统成熟
- 高层开发体验好

缺点，

- graph 灵活性不如 LangGraph
- 子代理隔离与 lineage 不如 codeg
- 对复杂 runtime 细节有一定隐藏

结论，

- **非常适合作为"高层多代理编程模型 + 统一记忆系统"参考**

---

### 1.10 AutoGen

仓库:<https://github.com/microsoft/autogen>
截至 2026-06-03，58651 stars，8854 forks，878 open issues，最近推送 2026-04-15。当前 README 明确标注 **maintenance mode**。

#### 项目定位与核心功能

AutoGen 是经典的多代理框架，当前仓库分层很清楚，

- `autogen-core`
- `autogen-agentchat`
- `autogen-ext`
- `autogen-studio`

README 已明确建议新项目优先考虑 Microsoft Agent Framework，但 AutoGen 仍然是极其重要的架构样本。

#### 架构设计模式

设计文档 `docs/design/01 - Programming Model.md` 非常关键，直接说明，

- 编程模型是 **publish-subscribe**
- 事件格式对齐 CloudEvents
- agent handler 基于 event type match

源码对应关系，

- `autogen_core/_single_threaded_agent_runtime.py`
- `autogen_core/_subscription.py`
- `autogen_core/_topic.py`
- `autogen_agentchat/teams/_group_chat/*`
- `autogen_ext/tools/mcp/_workbench.py`

这是非常标准的，

- **actor model**
- **event bus**
- **typed runtime**
- **high-level chat/team layer**

#### Agent 模型

AutoGen 的 agent 模型层次很完整，

- Core，事件驱动 actor
- AgentChat，AssistantAgent / UserProxy / SocietyOfMind / Teams
- Group Chat，RoundRobin / Selector / Swarm 等

这比 LangGraph 更直接面向多代理协作。

#### 工作流引擎

AutoGen 的 workflow 并不是 graph-first，而是，

- event-driven routing
- team patterns
- group chat managers

它可以表达复杂协作，但若要画成显式 DAG，不如 LangGraph / CrewAI Flow 自然。

#### 工具调用机制

Core 层有非常好的抽象，

- `tools/_workbench.py`，共享状态工具集的统一宿主
- `tool_agent/*`
- AgentChat `AgentTool`
- ext MCP workbench

`Workbench` 把"一个工具集合及其共享状态"抽象成一等对象，这一点很值得借鉴。

#### 记忆与上下文管理

Core 提供，

- `memory/_base_memory.py`
- `model_context/*`

其中 `TokenLimitedChatCompletionContext` 体现了 context window 管理能力。

ext 还提供，

- Chroma/Redis/Mem0 memory
- experimental task-centric memory

说明 AutoGen 对记忆采取的是，

- 内核给协议
- 扩展层给实现

#### 子代理机制

AutoGen 的子代理机制主要体现在，

- `AgentTool`
- team/group chat participants
- topic/subscription routing

它的强项是 **多代理消息协作**，而不是 isolated worktree child session。

#### 扩展机制

扩展能力很强，

- ext model clients
- ext tools
- MCP workbench
- memory backends
- grpc runtimes
- Studio

#### 优缺点分析

优点，

- 分层最清晰之一
- actor/event 模型适合分布式多代理
- Workbench 抽象很成熟

缺点，

- 当前维护模式削弱了长期基座价值
- workflow 图表达能力不如 LangGraph
- 对单机产品级 coding agent 场景不如 codeg/ClaudeCodeRev 贴身

结论，

- **适合作为"事件驱动多代理 runtime + workbench abstraction"参考**
- **要谨慎看待其长期演进风险**

---

## 2. 横向对比矩阵

说明，以下评价面向“可作为新 Agent 框架基座的参考价值”，不是单纯比较产品功能多寡。评分采用 `高 / 中 / 低`，其中“高”表示该能力在仓库中已经有清晰抽象和可复用实现，“中”表示有局部能力但未形成完整框架，“低”表示能力弱、缺位，或更多停留在 README，产品层。

### 2.1 能力矩阵，维度 1—5，

| 项目 | 类型 | 推理 | 执行 | 编排 | 协作 | 记忆 |
|---|---|---|---|---|---|---|
| SpectrAI | 桌面宿主 | 中 | 高 | 中低 | 中 | 低中 |
| desktop-cc-gui | 桌面宿主，治理工作台 | 中 | 中 | 低中 | 中低 | 高 |
| AionUi | 宿主平台，扩展生态 | 中 | 高 | 中低 | 中高 | 中低 |
| codeg | 多代理 coding workspace | 中 | 高 | 中 | 高 | 中低 |
| GenericAgent | 极简框架 | 中 | 中 | 低中 | 中 | 高 |
| ClaudeCodeRev | 恢复源码样本 | 中高 | 高 | 中 | 高 | 高 |
| LangGraph | 低层编排框架 | 中 | 高 | 高 | 中高 | 中高 |
| LangChain | 高层 Agent 平台 | 中 | 高 | 高 | 中 | 中高 |
| CrewAI | 多代理，Flow 框架 | 中高 | 高 | 高 | 高 | 高 |
| AutoGen | 事件驱动多代理框架 | 中高 | 高 | 中高 | 高 | 中高 |

### 2.2 能力矩阵，维度 6—10，

| 项目 | 上下文 | 运行时 | 安全与治理 | 集成 | 产品化 |
|---|---|---|---|---|---|
| SpectrAI | 中 | 中 | 中 | 高 | 高 |
| desktop-cc-gui | 高 | 中高 | 中高 | 中 | 高 |
| AionUi | 中 | 中 | 中高 | 高 | 高 |
| codeg | 中 | 高 | 中 | 高 | 中高 |
| GenericAgent | 中 | 中 | 低 | 中 | 中 |
| ClaudeCodeRev | 高 | 高 | 高 | 高 | 中高 |
| LangGraph | 中 | 高 | 中 | 高 | 中 |
| LangChain | 中 | 中高 | 中 | 高 | 中高 |
| CrewAI | 中高 | 高 | 中高 | 高 | 高 |
| AutoGen | 中 | 高 | 中高 | 高 | 中 |

### 2.3 按能力域的综合观察

1. **推理能力**，`CrewAI`、`AutoGen`、`ClaudeCodeRev` 更强调规划、反思、协作式决策，`LangGraph` 则故意把推理留给上层，自己只提供编排骨架。
2. **执行能力**，`LangChain`、`CrewAI`、`AutoGen`、`AionUi`、`codeg` 在工具或外部能力接入上最完整，`SpectrAI` 的价值主要在多 CLI Provider 适配与 MCP bridge。
3. **编排能力**，`LangGraph` 最强，`CrewAI Flow` 次之，`AutoGen` 偏事件与消息驱动，组合能力强但不如显式图直观。
4. **协作能力**，`AutoGen` 擅长群聊与事件协作，`CrewAI` 擅长角色协作，`codeg` 擅长子任务运行时，`SpectrAI` 的层级式 supervisor 最务实。
5. **记忆能力**，`GenericAgent` 的分层记忆模型最有启发，`CrewAI` 的统一记忆系统最工程化，`desktop-cc-gui` 在项目记忆与上下文治理上最产品化。
6. **上下文能力**，`desktop-cc-gui` 最值得借鉴，其次是 `ClaudeCodeRev`，多数框架在记忆与检索上投入较多，但对 token 预算、压缩和 context ledger 的治理还不够系统。
7. **运行时能力**，`LangGraph`、`AutoGen`、`codeg` 在 checkpoint、恢复、事件、长任务治理上更像真正的 runtime，这也是新架构最该优先吸收的部分。
8. **安全与治理**，`ClaudeCodeRev`、`AionUi`、`desktop-cc-gui` 更重视权限、审批、沙箱和审计，纯研究型框架普遍较弱。
9. **集成能力**，`LangChain` 的生态广度仍然最大，`AutoGen` 的 Workbench、`AionUi` 的 manifest、`codeg` 的 delegation MCP 则更偏运行时深度。
10. **产品化能力**，`SpectrAI`、`desktop-cc-gui`、`AionUi` 更接近完整产品宿主，它们不一定适合作为内核，但非常适合作为工作区、会话治理、可视化和远控能力的样本。

### 2.4 结论

如果只比较“抽象基座”，第一梯队仍是 `LangGraph`、`AutoGen Core`、`CrewAI`，如果比较“把 Agent 真正跑稳”，则 `codeg`、`desktop-cc-gui`、`AionUi`、`SpectrAI` 这些产品样本同样重要，因为它们提供了大量框架文档里不写、但在真实系统里必须存在的运行时细节。

---

## 3. 最佳实践提炼

### 3.1 最有效的架构设计模式

#### 模式 A，内核最小化，外围能力插件化

最佳参考，

- `LangGraph`
- `AutoGen Core`
- `AionUi`扩展 manifest 方向

结论，

- 核心只保留，runtime、state、tool/workbench protocol、memory protocol、checkpoint protocol、agent protocol
- provider、skills、tools、MCP、UI、remote bridge 全部外置

#### 模式 B，执行状态与业务状态解耦

最佳参考，

- `LangGraph`，state vs checkpoint
- `codeg`，delegation runtime vs conversation DB
- `desktop-cc-gui`，runtime ledger vs project memory vs context ledger

结论，

- 不要把"用户任务状态""会话状态""代理状态""工作流状态"混成一个状态机
- 需要至少四套状态，
  - 会话状态
  - 工作流状态
  - 子任务状态
  - 记忆状态

#### 模式 C，子代理必须是 runtime 一等实体

最佳参考，

- `codeg`
- `ClaudeCodeRev`
- `SpectrAI`

结论，

- 子代理不能只是一段 prompt
- 必须有，
  - `agent_id`
  - `parent_id`
  - `task_id`
  - `status`
  - `workspace/isolation`
  - `result channel`

### 3.2 工作流编排最佳实践

应吸收，

- `LangGraph` 的 `StateGraph + Command + Send + checkpoint + interrupt`
- `CrewAI Flow` 的装饰器 DSL、持久化与 human feedback

不建议，

- 像 `GenericAgent` 那样完全靠 SOP 文本驱动复杂流程
- 像某些产品仓库那样只在 README 中定义 DAG、源码里没有真正 runtime

落地建议，

- 内核使用显式 graph runtime
- 上层提供 DSL / YAML / visual editor
- 子工作流编译为节点
- 节点返回 `update + goto + spawn + wait + interrupt`

### 3.3 记忆管理最佳方案

建议综合三家的长处，

- `GenericAgent`，分层认知模型
- `CrewAI`，统一存储与 recall flow
- `desktop-cc-gui`，上下文注入治理与预算管理

推荐统一为 5 层记忆体系，

- **Working Memory**，当前任务 scratchpad、约束、未决假设、短期计划
- **Episodic Memory**，会话与任务结果、turn summaries、tool traces、决策过程
- **Semantic Memory**，稳定事实、用户偏好、项目知识、团队约定
- **Procedural Memory**，SOP、skills、workflow templates、tool recipes
- **Artifact Memory**，文件、代码 diff、报告、图片、测试结果等可复用产物

这里需要特别强调，`Artifact Memory` 不是 `Episodic Memory` 的附件，而是独立层。因为很多真实任务恢复的关键，不是“之前发生过什么”，而是“之前产出了什么”。

### 3.4 子代理协作最佳模式

不要只选一种，应系统性地支持四种协作模式，以覆盖不同任务场景，

#### 模式 A，Supervisor-Worker主管-工人

适合复杂任务拆解与验收。

```
Supervisor Agent
 ├── Research Agent
 ├── Coding Agent
 ├── Testing Agent
 └── Review Agent
```

特点，
- 一个主管 Agent 负责任务拆解、分配和验收
- 多个 Worker Agent 负责具体执行
- 天然支持子任务并行
- 适合软件开发、数据分析、多步研究

最佳参考，`CrewAI` hierarchical process、`SpectrAI` spawnAgent/waitAgent

#### 模式 B，Group Chat群聊协商

适合讨论型、评审型任务。

```
Agent A ↔ Agent B ↔ Agent C ↔ User
         ↑
      Moderator / Router
```

特点，
- 多 Agent 多轮对话
- 有 Moderator 或 Router 决定谁发言
- 支持辩论、投票、共识形成
- 适合方案讨论、代码评审、创意头脑风暴

最佳参考，`AutoGen` GroupChat / Swarm、`CrewAI` role delegation

#### 模式 C，Pipeline流水线

适合稳定、可复用的流程。

```
需求分析 → 设计 → 编码 → 测试 → 文档 → 发布
```

特点，
- 顺序明确，阶段间有清晰输入输出
- 每个阶段可独立优化和替换
- 可复用，适合反复执行的标准流程
- 适合企业流程、代码生成流程、报告生成流程

最佳参考，`CrewAI Flow`、`LangGraph` 线性/DAG graph

#### 模式 D，Blackboard / Task Board任务板

适合长期持续推进的异步任务。

```
Task Board
 ├── todo
 ├── in_progress
 ├── blocked
 ├── review
 └── done
```

特点，
- Agent 从共享任务池中自主领取或被分配任务
- 支持中断、恢复、长期运行
- 多 Agent 可并行工作，任务间有依赖关系
- 适合异步任务、项目型任务、持续交付

最佳参考，`codeg` delegation broker、`AionUi` mailbox / team_tasks、`SpectrAI`规划中

#### 模式选择指南

| 场景 | 推荐模式 | 说明 |
|---|---|---|
| 一次性任务拆解 | Supervisor-Worker | 有明确目标，需分解后验收 |
| 方案讨论/辩论 | Group Chat | 需要多视角碰撞 |
| 标准化流程执行 | Pipeline | 阶段稳定、顺序固定 |
| 长期项目推进 | Blackboard/Task Board | 异步、可中断、可恢复 |

与 v1 相比，这里把原先笼统的“三类子代理模式”收敛为 4 种标准协作模式。实践中这 4 种模式可以组合使用，例如 Supervisor 可以通过 Pipeline 管理内部子任务，也可以通过 Task Board 进行长期任务分配。

#### 生命周期形态补充

除上述 4 种协作模式外，子代理本身的生命周期形态也需要区分，

- **一次性one-shot**，适合 code review、research、file scan
- **持久会话式persistent**，适合长任务协作、连续问答
- **团队成员式team_member**，适合多角色业务流程中固定角色

借鉴对象，

- `codeg`，one-shot async delegation
- `SpectrAI`，persistent child session + idle wait
- `CrewAI`，role delegation
- `AutoGen`，group chat / swarm

### 3.5 工具系统设计最佳实践

应同时具备四层，

1. **Base Tool Protocol**
2. **Workbench / Tool Group**
3. **Adapter Layer**
4. **Policy / Permission / Result Normalization**

借鉴来源，

- `AutoGen Workbench`
- `LangChain core tools`
- `SpectrAI` Provider Adapter
- `AionUi` manifest permissions

关键原则，

- 工具不是裸函数，而是带 metadata、side effects、streaming、权限声明的对象
- agent-to-agent 调用也应被建模成工具
- CLI/MCP/HTTP/SDK 工具应该走统一 result envelope

---

### 3.6 架构设计原则与核心洞察

以下 10 条架构设计原则和能力地图方法论，提炼自对 10 个项目的交叉分析与讨论记录，可作为新架构设计的顶层约束。

### 3.6.1 十条架构设计原则

**原则 1，一切都是 NodeEverything is a Node**

LLM、Tool、Agent、Workflow、Human、Memory 操作，全部都可以作为 Workflow Node。这是整个架构**递归可组合性**的基础。

```ts
type WorkflowNode =
  | LLMNode          // LLM 推理
  | ToolNode         // 工具调用
  | AgentNode        // 子代理执行
  | WorkflowNode     // 子工作流嵌入
  | RouterNode       // 条件路由
  | MemoryNode       // 记忆读写
  | HumanApprovalNode // 人类确认
  | CodeExecutionNode // 代码执行
  | EvaluationNode   // 结果评估
```

这意味着，
- Agent 可以作为节点嵌入 Workflow
- Workflow 可以作为节点嵌入上层 Workflow递归组合
- Tool 可以被 Agent 调用，Agent 本身也可以被包装成 Tool
- Human 也是一等节点，不是事后附加的 UI 交互

**原则 2，一切执行都产生 Event，Event Sourcing，**

每次模型调用、工具调用、状态变化、记忆写入、权限检查都应记录为不可变事件。事件是系统可观测性、可审计性、可恢复性的基础，详见 §4.3.1 与 §4.3.7，。

**原则 3，Workflow 是 Graph，不只是 Chain**

必须支持循环、条件分支、并行、子图、暂停、恢复。简单线性 Chain 只是一个特例，不应成为架构上限。

**原则 4，Agent 是可组合单元，不是固定类**

Agent 可以被调用作为 Tool，也可以调用别人通过 AgentNode。Agent 没有固定的层级关系，可以自由嵌套。

**原则 5，Tool 是统一外部能力接口**

CLI、HTTP、函数、MCP、数据库、浏览器、其他 Agent，都应该包装为统一接口的 Tool。Agent 通过 Tool 与外部世界交互，而不是通过特殊 API。

**原则 6，Memory 和 Context 分离**

Memory 是存储层，用于长期保存信息。Context 是运行时输入层，只表示本次模型调用实际放进去的内容。详见 §4.3.5、§4.3.6 与 §4.8.7。

**原则 7，状态必须持久化Durable Execution**

长任务必须能 checkpoint / resume / cancel / retry。系统崩溃、模型超时、人工介入都不是异常终止，而是正常状态。

**原则 8，默认安全**

所有危险工具都需要权限、沙箱、审计。工具 metadata 中应声明副作用等级、是否需要确认、哪些 Agent 可以调用。

**原则 9，可观察性是核心能力，不是附加功能**

复杂 Agent 如果不可调试就不可用。日志、Trace、事件回放、成本追踪、上下文归因都必须内建。

**原则 10，先做小核心，再做生态**

核心抽象越稳定，后面越容易扩展。不要一开始就追求"完美实现所有能力"。

> 如果核心抽象设计得当，复杂能力会自然长出来。但如果核心抽象设计错误，后期重构的代价比从一开始就做对要大十倍。

### 3.6.2 能力地图，Capability Map，方法论

不要只列功能，而应按能力域归纳，形成系统性的能力地图。这既是架构设计的分析框架，也是评估项目覆盖度的标尺，§1 的项目模板与 §2 的横向矩阵都应回到这 10 个维度上。

```
Agent Capability Map

1. Reasoning(推理)
   - planning(规划)
   - reflection(反思)
   - critique(审查)
   - verification(验证)

2. Acting(执行)
   - tool calling(工具调用)
   - cli execution(CLI 执行)
   - browser control(浏览器控制)
   - file operation(文件操作)
   - code execution(代码执行)

3. Orchestration(编排)
   - graph workflow(图工作流)
   - sub workflow(子工作流)
   - parallel execution(并行执行)
   - conditional routing(条件路由)
   - retry / compensation(重试/补偿)
   - human in the loop(人类介入)

4. Collaboration(协作)
   - sub agent(子代理)
   - group chat(群聊)
   - supervisor-worker(主管-工人)
   - task board(任务板)
   - delegation(委派)

5. Memory(记忆)
   - short-term memory(短期记忆)
   - long-term memory(长期记忆)
   - semantic memory(语义记忆)
   - episodic memory(情景记忆)
   - artifact memory(产物记忆)

6. Context(上下文)
   - retrieval(检索)
   - compression(压缩)
   - summarization(摘要)
   - prioritization(优先级排序)
   - token budgeting(Token 预算管理)

7. Runtime(运行时)
   - event log(事件日志)
   - checkpoint(检查点)
   - resume(恢复)
   - cancel(取消)
   - streaming(流式输出)
   - trace(追踪)

8. Safety(安全)
   - permission(权限)
   - sandbox(沙箱)
   - approval(审批)
   - secret masking(密钥脱敏)
   - audit log(审计日志)

9. Integration(集成)
   - API
   - CLI
   - MCP
   - file system
   - git
   - browser
   - database

10. Productization(产品化)
    - UI
    - workspace
    - session
    - project
    - history
    - visualization
```

这 10 大能力域既是评估项目覆盖度的标准维度，也是新架构模块划分的参考依据。后文的模块设计、RunState、Memory/Context 分工，都应能映射回这 10 个能力域，而不是另起一套分类法。

### 3.6.3 “一切都是 Node”递归组合原则详解

这是 v1 中提到但未充分展开的核心原则，在讨论记录中被反复验证为最关键的架构决策。

**核心理念，**

> Agent 是一种 Workflow，Workflow 也是一种 Tool，Tool 可以调用 Agent，Agent 可以包含子 Agent，子 Agent 可以执行子 Workflow。

这种递归组合性带来的优势，

1. **表达力无限**，任何复杂流程都可以通过节点组合表达
2. **可复用**，一个 Agent 或 Workflow 可以被多处引用
3. **可测试**，每个节点可以独立测试
4. **可替换**，换一个 Agent 实现不影响整个 Workflow 结构

**具体表现，**

```yaml
# Agent 作为节点嵌入 Workflow
nodes:
  - id: plan
    type: agent
    agent: planner_agent
  - id: research
    type: agent
    agent: research_agent
  - id: code
    type: agent
    agent: coding_agent
  - id: review
    type: subworkflow        # Workflow 作为节点
    workflow: review_flow
  - id: approve
    type: human              # Human 也是节点
    prompt: "是否应用修改?"
  - id: remember
    type: memory             # Memory 操作也是节点
    action: save
    scope: project
```

**递归组合的层次，**

```
Workflow (顶层)
  ├── Agent Node
  │    └── Agent 的内部 Workflow(Agent Loop 本身就是一个 Workflow)
  │         ├── LLM Node
  │         ├── Tool Node
  │         └── Agent Node (子代理)
  ├── Workflow Node
  │    └── Sub-Workflow(递归嵌套)
  │         ├── Agent Node
  │         └── Tool Node
  └── Human Node
```

**与 v1 的区别，**

v1 在 §4.3.2 和 §4.4.2 中定义了节点类型，但没有把**递归组合**提升为顶层原则。本版将其放到最前，是因为复杂工作流组合、子代理使用、子工作流嵌套、人类介入与记忆读写的统一建模，都以此为前提。

### 3.7 关键洞察，98.4% Harness vs 1.6% Intelligence

这一洞察来自对 Claude Code，ClaudeCodeRev 逆向分析样本，的深度观察。

#### 发现

对 Claude Code 2.1.88 源码的逆向分析揭示了一个惊人但符合直觉的事实，

> **98.4% 的代码库是确定性操作框架harness，而非 AI 决策逻辑。**

具体来说，
- 工具系统、权限系统、文件管理、进程管理、会话管理、UI 集成等确定性代码占据了绝大部分
- 真正涉及 LLM 调用、决策、推理的代码只是极小的一部分

#### 架构启示

这一发现对新架构设计的含义非常深远，

1. **框架比模型更关键**，构建一个优秀的 Agent 系统，核心竞争力在于那 98.4% 的“操作框架”，而不是在于用了什么模型
2. **harness 的质量决定上限**，即使有最好的模型，如果操作框架，工具管理、状态管理、错误恢复、安全控制，做得不好，Agent 的能力也会被严重限制
3. **“小核心 + 大生态”策略得到验证**，核心 AI 决策逻辑只需最小内核，绝大部分工程量在框架基础设施上
4. **关注点分离的必要性**，确定性代码和 AI 决策代码应该清晰分离，前者需要传统工程严谨性，后者需要灵活迭代

#### 对 v4.12 实现路线的影响

这意味着 MVP 的优先级应该重排，

- **最高优先级**，Runtime Kernel、Workflow Engine、Tool Runtime、State / Event 体系。这些都是 harness 的一部分。
- **中等优先级**，Agent Loop、Context Manager、Memory。这些是 harness 和 intelligence 的交界。
- **较低优先级**，高级 Planner、自动优化。这些属于 1.6% 中的增强部分。

---

## 4. 新架构设计方案

### 4.1 设计目标

目标不是复制任一现有项目，而是组合出一套，

- 核心抽象干净，接近 `LangGraph + AutoGen Core`
- 多代理 runtime 能力接近 `codeg + SpectrAI`
- 记忆体系吸收 `GenericAgent + CrewAI + desktop-cc-gui`
- 扩展机制吸收 `AionUi + LangChain ecosystem`

### 4.2 总体架构图，文字描述，

```text
┌──────────────────────────────────────────────────────────────┐
│                        Client / API Layer                    │
│  Chat UI / CLI / HTTP API / Scheduler / Remote Controller   │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                    Conversation & Task Hub                   │
│  ThreadState / TaskState / Progress / Interrupt / Resume     │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                    Workflow Graph Runtime                    │
│  Graph / Node / Edge / Command / Send / Checkpoint / Retry   │
└──────────────────────────────────────────────────────────────┘
             │                    │                    │
             ▼                    ▼                    ▼
┌──────────────────┐   ┌────────────────────┐  ┌────────────────────┐
│  Agent Runtime   │   │    Tool Runtime    │  │    Memory Facade    │
│  spawn/send/wait │   │ Tool/Workbench/MCP │  │ WM/EM/SM/PM layers  │
│  team/mailbox    │   │ CLI/HTTP/SDK/Agent │  │ retrieval/compress   │
└──────────────────┘   └────────────────────┘  └────────────────────┘
             │                    │                    │
             └──────────────┬─────┴────────────┬──────┘
                            ▼                  ▼
                 ┌────────────────────┐  ┌────────────────────┐
                 │ Persistence Layer  │  │ Observability      │
                 │ DB / KV / VectorDB │  │ Events / Traces    │
                 │ Checkpoints / FTS  │  │ Cost / Governance  │
                 └────────────────────┘  └────────────────────┘
```

### 4.3 模块划分

#### 4.3.1 `kernel.runtime`

职责，

- 生命周期
- 事件循环
- 调度
- checkpoint
- retry / timeout / cancel

来源借鉴，

- `LangGraph Pregel`
- `AutoGen runtime`

核心要求，

- 单机可跑
- 分布式可替换
- 不绑定任何特定模型或工具系统

`kernel.runtime` 应以事件溯源，Event Sourcing，作为核心执行模型。每次状态变更、工具调用、Agent 决策、记忆写入都应作为不可变事件写入事件日志，Event Store，。这带来的核心优势如下，

- **可回放**，任何执行过程都可以从事件日志完整重建
- **可审计**，完整的操作审计链，支持安全审查和合规
- **可恢复**，系统崩溃后从最后一个 checkpoint + 事件日志恢复
- **可调试**，通过事件回放定位 Agent 行为异常的根因
- **可分析**，基于事件流的成本追踪、性能分析、行为模式挖掘
>
> 事件模型建议，
> ```ts
> interface RuntimeEvent {
>   eventId， string;
>   eventType， string;  // "model_call" | "tool_call" | "state_change" | "memory_write" | ...
>   runId， string;
>   nodeId?， string;
>   agentId?， string;
>   payload， unknown;
>   timestamp， string;
>   causalId?， string;  // 因果链，用于关联上下游事件
> }
> ```

#### 4.3.2 `workflow.graph`

职责，

- 定义 graph、node、edge、subgraph
- 支持线性、DAG、条件、循环、嵌套、动态路由
- 提供节点注册表，允许内建节点与扩展节点并存

核心对象，

- `WorkflowGraph`
- `WorkflowNode`
- `ExecutionCommand`
- `StateReducer`

`workflow.graph` 模块不仅是编排引擎，更是整个系统递归可组合性的实现基础。任何能力，Agent、Tool、Workflow、Human、Memory 操作，都可以作为 `WorkflowNode` 挂入图中。这意味着，

> - Agent 的 Agent Loop 本身就是一个内建 Workflow
> - Workflow 可以嵌套为 `SubWorkflowNode`，见 §3.6.3，
> - Tool 调用是 `ToolNode`
> - Memory 读写是 `MemoryNode`
> - 人类确认是 `HumanApprovalNode`
>
> 这种“一切都是 Node”的设计使得复杂流程可以自然表达，而不需要再发明一套特殊的硬编码机制。

#### 4.3.3 `agent.runtime`

职责，

- agent 定义
- child agent spawn
- mailbox / taskboard / team session
- session isolation

支持三种子代理模式，

- `oneshot`
- `persistent`
- `team_member`

#### 4.3.4 `tool.runtime`

职责，

- tool registry
- workbench
- adapter
- streaming result
- permission & policy

支持工具类型，

- local function tool
- MCP tool
- CLI tool
- HTTP tool
- SDK tool
- agent tool

#### 4.3.5 `memory.system`

职责，

- working memory
- episodic memory
- semantic memory
- procedural memory
- artifact memory
- memory write policy
- memory retrieval
- forgetting / retention policy

`memory.system` 只负责“存什么、怎么取、怎么忘”，不负责“本次 prompt 怎么组”。上下文预算、组装、压缩全部移交给独立的 `context.manager`。

#### 4.3.6 `context.manager`

职责，

- retrieval pack composition
- context budget planning
- prompt assembly
- summarization / compression
- context ranking / prioritization

`context.manager` 负责把任务状态、近期消息、工具结果和来自 `memory.system` 的候选内容装配成真正送给模型的运行时输入。它与 `memory.system` 的边界是，

- `memory.system` 返回候选知识、历史摘要、可引用产物
- `context.manager` 决定哪些内容进入本次上下文，以及各分区占用多少 token

#### 4.3.7 `state.persistence`

职责，

- thread/task/agent/workflow state
- run state snapshot
- checkpoint
- event log
- FTS transcripts
- vector memory

#### 4.3.8 `extension.sdk`

职责，

- plugin manifest
- capability contribution
- permission declaration
- schema validation

#### 4.3.9 `observability.governance`

职责，

- event trace
- tool/audit log
- cost ledger
- context ledger
- safety gates

### 4.4 核心接口定义

以下用 TypeScript 风格伪代码描述。

#### 4.4.1 Agent 定义

```ts
type AgentMode = "oneshot" | "persistent" | "team_member";
type IsolationMode = "shared" | "worktree" | "sandbox" | "remote";

interface AgentSpec {
  id: string;
  name: string;
  role?: string;
  description?: string;
  systemPrompt?: string;
  modelRef: string;
  toolset: string[];
  memoryProfile: string;
  mode: AgentMode;
  isolation: IsolationMode;
  permissions: PermissionPolicy;
}
```

#### 4.4.2 工作流节点

```ts
type BuiltinNodeKind =
  | "llm"
  | "tool"
  | "agent"
  | "router"
  | "human_approval"
  | "subworkflow"
  | "memory"
  | "code_execution"
  | "evaluation"
  | "script";

type WorkflowNodeKind = BuiltinNodeKind | `ext:${string}`;

interface WorkflowNode<State> {
  id: string;
  kind: WorkflowNodeKind;
  registryKey?: string; // kind = ext:* 时用于查找注册实现
  inputSchema?: JsonSchema;
  outputSchema?: JsonSchema;
  inputMapping?: Mapping;
  outputMapping?: Mapping;
  run(ctx: NodeContext<State>): Promise<NodeResult<State>>;
}

interface NodeResult<State> {
  update?: Partial<State>;
  command?: ExecutionCommand<State>;
  events?: RuntimeEvent[];
  artifacts?: ArtifactRef[];
}
```

#### 4.4.3 执行命令

```ts
type ExecutionCommand<State> =
  | { type: "goto"; to: string }
  | { type: "branch"; routes: string[] }
  | { type: "spawn_agent"; agent: AgentSpec; task: string; wait?: boolean }
  | { type: "await_task"; taskId: string; timeoutMs?: number }
  | { type: "interrupt"; reason: string; resumable: boolean }
  | { type: "finish"; output?: unknown };
```

全文统一使用 `await_task` 表示“等待某个子任务完成并在完成后恢复执行”，不再混用 `wait_task` 与 `await_task`。

#### 4.4.4 统一执行状态对象，RunState，

```ts
type RunStatus =
  | "pending"
  | "running"
  | "paused"
  | "failed"
  | "completed"
  | "cancelled";

interface RunState<State = Record<string, unknown>> {
  runId: string;
  threadId?: string;
  workflowId?: string;
  workflowVersion?: string;
  agentId?: string;
  taskId?: string;
  status: RunStatus;
  currentNodeId?: string;
  variables: State;
  messages: MessageRecord[];
  toolCalls: ToolCallRecord[];
  artifacts: ArtifactRef[];
  tasks: TaskSnapshot[];
  memoryRefs: MemoryRef[];
  checkpoints: CheckpointRef[];
  events: RuntimeEvent[];
  startedAt: string;
  updatedAt: string;
}
```

`RunState` 的意义不是再造一份“超级状态”，而是为 workflow、task、message、tool call、artifact、memory ref、event 这些执行实体提供统一的串联视图。它既服务于恢复，也服务于调试、审计和人类介入。

#### 4.4.5 工具系统

```ts
interface ToolSchema {
  name: string;
  description: string;
  input: JsonSchema;
  outputMode: "text" | "json" | "stream" | "artifact";
  sideEffectLevel: "read" | "write" | "network" | "exec";
  permissions?: string[];
}

interface Tool {
  schema(): ToolSchema;
  call(input: unknown, ctx: ToolContext): Promise<ToolResult>;
}

interface Workbench {
  listTools(): Promise<ToolSchema[]>;
  callTool(name: string, input: unknown, ctx: ToolContext): Promise<ToolResult>;
  saveState?(): Promise<Record<string, unknown>>;
  loadState?(state: Record<string, unknown>): Promise<void>;
}
```

#### 4.4.6 记忆与上下文接口

```ts
interface MemoryFacade {
  working: WorkingMemory;
  episodic: EpisodicMemory;
  semantic: SemanticMemory;
  procedural: ProceduralMemory;
  artifact: ArtifactMemory;

  persistTurn(input: TurnRecord): Promise<void>;
  retrieve(input: RetrievalRequest): Promise<RetrievalPack>;
  writeArtifact(input: ArtifactWriteRequest): Promise<ArtifactRef>;
}

interface ContextManager {
  buildContext(input: ContextBuildRequest): Promise<ModelContext>;
  compact(input: ContextCompactionRequest): Promise<CompactionResult>;
  allocateBudget(input: BudgetPlanRequest): Promise<BudgetPlan>;
  recordLedger(input: ContextLedgerEntry): Promise<void>;
}
```

这里故意不让 `MemoryFacade` 提供 `compactContext` 之类的方法，以避免再次把存储层与运行时输入层揉在一起。

#### 4.4.7 结构化输入输出的重要性

讨论记录反复强调，Agent、Tool、Workflow Node 的输入输出都应 schema 化，而非自由文本。

**为什么结构化 IO 至关重要，**

```json
// 非结构化输出，仅文本，，
"代码里有 3 个问题，第一个是 xxx，第二个是 yyy……"

// 结构化输出，JSON Schema，，
{
  "summary": "string",
  "issues": [
    {
      "severity": "high | medium | low",
      "file": "string",
      "line": "number",
      "message": "string",
      "suggestion": "string"
    }
  ],
  "need_fix": "boolean"
}
```

**结构化输出的 6 大好处，**

1. **更容易组合工作流**，下游节点可以根据结构化字段做条件判断
2. **更容易判断条件**，`condition， "result.need_fix == true"` 比正则匹配可靠得多
3. **更容易回放调试**，每个节点的输入输出都有明确的数据契约
4. **更容易测试**，可以构造固定输入，断言输出结构
5. **更容易替换模型**，只要输出 schema 不变，换模型不影响下游
6. **更容易接 UI**，前端可以根据 schema 自动渲染结果

**在接口设计中的体现，**

```ts
// AgentSpec 和 ToolSpec 中都应定义
interface AgentSpec {
  // ...
  inputSchema?: JsonSchema;   // Agent 接受的结构化输入
  outputSchema?: JsonSchema;  // Agent 应输出的结构
}

interface ToolSpec {
  // ...
  inputSchema: JsonSchema;
  outputSchema?: JsonSchema;
}
```

### 4.5 数据模型建议

建议至少落以下几类存储，

#### 4.5.1 事务型数据库表

- `threads`
- `turns`
- `tasks`
- `task_dependencies`
- `workflow_runs`
- `workflow_steps`
- `run_state_snapshots`
- `agents`
- `agent_sessions`
- `agent_messages`
- `mailbox`
- `task_claims`
- `tool_calls`
- `checkpoints`
- `artifacts`
- `artifact_links`
- `context_ledgers`

#### 4.5.2 检索型存储

- `transcript_fts`
- `memory_vectors`
- `skill_index`
- `artifact_index`

#### 4.5.3 事件日志

- `runtime_events`
- `audit_events`
- `cost_events`
- `context_events`

建议把 `run_state_snapshots` 视为 `RunState` 的序列化快照，而不是再为 workflow、task、message、tool call 各自维护互不相干的恢复逻辑。恢复时优先读取最近快照，再回放 `runtime_events` 补齐尾部事件。

### 4.6 工作流引擎设计

#### 4.6.1 执行模型

建议采用 **typed shared state + command-driven routing**，

- 节点只做局部更新
- reducer 负责并发聚合
- 路由通过 command 显式返回
- subflow 编译后可当普通节点挂入父图

#### 4.6.2 支持的流程形态

必须原生支持，

- 线性链
- 扇出/扇入 DAG
- 条件分支
- while / until 循环
- 子工作流嵌套
- 动态路由
- 中断等待人工恢复

#### 4.6.3 恢复策略

每一步结束后可配置 durability，

- `sync`
- `async`
- `exit`

这部分直接吸收 LangGraph 的经验即可。

### 4.7 多代理协作设计

#### 4.7.1 子代理生命周期

必须有清晰状态机，

- `pending`
- `starting`
- `running`
- `waiting_input`
- `idle`
- `completed`
- `failed`
- `cancelled`

#### 4.7.2 通信协议

建议同时支持三种通信，

- **direct message**，点对点 mailbox
- **taskboard claim**，共享任务认领
- **event broadcast**，团队广播

#### 4.7.3 任务分配策略

支持，

- round robin
- capability match
- model affinity
- cost-aware routing
- dependency-aware routing

#### 4.7.4 隔离策略

必须支持，

- shared workspace
- git worktree
- sandbox tmpdir
- remote worker

这里应综合 `codeg`、`SpectrAI`、`ClaudeCodeRev` 的经验。

### 4.8 记忆系统设计

#### 4.8.1 Working Memory

用途，

- 当前目标
- 关键约束
- 已验证事实
- 待办/阻塞

要求，

- 小而稳定
- 每轮自动更新
- 不直接混入长期记忆

#### 4.8.2 Episodic Memory

用途，

- 历史任务
- 会话摘要
- 工具调用轨迹
- 人工纠正记录

#### 4.8.3 Semantic Memory

用途，

- 用户偏好
- 项目事实
- 稳定知识
- 决策结论

#### 4.8.4 Procedural Memory

用途，

- SOP
- skill
- workflow template
-- repair recipe

#### 4.8.5 Artifact Memory

用途，

- 文件
- 代码 diff
- 报告
- 图片
- 测试结果
- 中间数据产物

要求，

- 支持引用而不必全文内联到 prompt
- 支持版本、来源、关联任务与关联事件
- 能作为恢复与审计证据重新加载

`Artifact Memory` 的定位是“可复用产物层”，它通常来自工具执行、工作流节点输出或人工上传，不应该被降格为普通附件。

#### 4.8.6 上下文窗口智能管理

这部分属于 `context.manager`，但必须和记忆系统一起设计，因为上下文组装高度依赖记忆检索结果。

建议流程，

1. 先装入 thread recent window
2. 注入 working memory
3. 检索 semantic、procedural、artifact candidates
4. 根据任务意图做 relevance ranking
5. 结合 token budget 生成 retrieval pack
6. 若超限，优先压缩 episodic，最后才压缩关键约束

这部分应吸收，

- `desktop-cc-gui` 的 context ledger 思想
- `CrewAI RecallFlow`
- `GenericAgent` 的 working checkpoint

#### 4.8.7 Memory vs Context 分离原则

这是讨论记录中反复强调的核心概念区分，对架构设计有直接影响。

很多 Agent 系统将“Memory”和“Context”混为一谈，导致架构上产生以下问题，

- 上下文膨胀，把所有历史都塞进 model input
- 记忆和对话耦合，无法独立管理长期知识
- 检索策略混乱，分不清哪些是“该记住的”，哪些是“该放进 prompt 的”

**严格区分，**

| 概念 | 定义 | 生命周期 | 管理策略 |
|---|---|---|---|
| **Memory** | 系统长期保存的信息存储层 | 持久化，跨会话存在 | 写入策略、遗忘策略、索引策略 |
| **Context** | 本次模型调用实际放入 prompt 的运行时输入层 | 单次调用，用完即弃 | 组装策略、压缩策略、预算控制 |

**流程应该是，**

```
Task → Retrieve from Memory → Select Relevant Items
     → Prioritize & Rank → Compress if needed
     → Build Context → Call Model
```

**而不是，**

```
把所有历史消息都塞给模型
```

**架构影响，**

1. **Memory Manager 和 Context Manager 应该是两个独立模块**。前者属于 `memory.system`，后者属于 `context.manager`。
2. **Memory 负责存储、索引、检索、遗忘**，关注“知道什么”。
3. **Context Manager 负责组装、压缩、预算分配、格式化**，关注“告诉模型什么”。
4. **两者之间通过 Retrieval Pack 作为桥梁**。Memory 返回候选集，Context Manager 决定最终放什么。

**Memory 的 5 层 vs Context 的分区，**

Memory 分 5 层，详见 §4.8.1—§4.8.5，，

```
Memory Store，存储层，
├── Working Memory    当前任务 scratchpad
├── Episodic Memory   历史事件、会话摘要
├── Semantic Memory   稳定知识、用户偏好
├── Procedural Memory  SOP、skill、recipe
└── Artifact Memory   产物，文件、代码、报告
```

Context 是单次 model call 的 prompt 组成部分，

```
Model Context，运行时输入层，
├── System Instructions      系统指令
├── Developer Instructions   开发者指令
├── Agent Role / Goal        代理角色与目标
├── Current Task             当前任务描述
├── Workflow State           工作流当前状态
├── Retrieved Knowledge      从 Memory 检索出的相关内容
├── Recent Conversation      最近对话历史
├── Tool Results             工具调用结果
└── Output Format            输出格式要求
```

Context Manager 需要为每个分区精细控制 token 预算，并根据任务优先级动态调整分配。

### 4.9 工具生态设计

#### 4.9.1 注册机制

工具注册信息至少包含，

- name
- schema
- category
- side effect
- permission requirement
- streaming support
- stateful/stateless
- source， builtin/plugin/mcp/agent/cli

#### 4.9.2 调用链

建议统一成，

`LLM ToolCall -> Tool Router -> Adapter/Workbench -> Normalized Result -> Artifact/Event Store -> LLM Reflection`

#### 4.9.3 结果标准化

返回值不要只用字符串，应统一支持，

- text
- structured json
- diff
- file artifact
- image
- stream chunks
- error envelope

#### 4.9.4 Agent 作为工具

必须内建，

- `spawn_agent`
- `await_task`
- `cancel_task`
- `send_message_to_agent`

因为真实复杂任务里，agent-to-agent 调用比普通函数调用更关键。

#### 4.9.5 Agent，CLI，Workflow 三种 Adapter 统一设计

这里采用讨论中提出的重要抽象，将外部能力统一包装为三种一等 Adapter，使上层 Agent 可以用同一种方式调用不同类型的外部能力。

在真实 Agent 系统中，需要调用的外部能力类型多种多样，其他 Agent，如 Claude Code，、CLI 工具，如 git，、预定义工作流，如 `code_review`，。如果每种都设计不同的调用机制，系统会变得非常复杂。

**解决方案**，将所有外部能力统一包装为三种 Adapter，它们都实现统一的 Tool 接口，

```ts
// 1. AgentAdapter:把任何外部 Agent 包装成 Tool
interface AgentAdapter extends Tool {
  name: string;
  command: string;           // 如 "claude", "codex"
  mode: "interactive" | "oneshot";
  inputProtocol: "text" | "structured";
  outputParser: OutputParser;
}

// 2. CLIAdapter:把命令行工具包装成 Tool
interface CLIAdapter extends Tool {
  name: string;
  command: string;           // 如 "git"
  allowedArgs: string[];     // 如 ["status", "diff", "log"]
  cwdPolicy: "workspace" | "any" | "sandbox";
  requireApproval: boolean;
}

// 3. WorkflowAdapter:把预定义工作流包装成 Tool
interface WorkflowAdapter extends Tool {
  name: string;
  workflowId: string;        // 引用已注册的工作流
  inputMapping?: Mapping;     // 输入映射
  outputMapping?: Mapping;    // 输出映射
}
```

**上层 Agent 统一调用方式，**

```ts
// 调用 CLI 工具
await agent.callTool("git.status")

// 调用其他 Agent
await agent.callTool("claude_code.fix_bug")

// 调用工作流
await agent.callTool("workflow.code_review")
```

**与“一切都是 Node”原则的协同，**

这三种 Adapter 包装后，都可以作为 `ToolNode` 挂入 `WorkflowGraph`。更进一步，`AgentAdapter` 包装的 Agent 本身也是一个 Workflow，所以它又可以包含 `AgentNode`、`ToolNode` 等。这就是递归组合的具体体现。

**与 v1 §4.9.4 的关系，**

v1 提到了“Agent 作为工具”，但仅限于 `spawn_agent` 等内建工具。本版将其扩展为一个通用模式，任何外部能力，Agent、CLI、Workflow，都可以通过统一的 Adapter 模式成为一等可调用能力。

### 4.10 扩展点设计

建议采用 manifest 驱动，

```json
{
  "name": "my-plugin",
  "version": "1.0.0",
  "permissions": {
    "filesystem": "workspace-only",
    "network": true,
    "exec": false
  },
  "contributes": {
    "tools": ["./tools.json"],
    "agents": ["./agents.json"],
    "skills": ["./skills.json"],
    "mcpServers": ["./mcp.json"],
    "workflows": ["./workflows.json"]
  }
}
```

扩展点建议分为，

- tool provider
- agent preset
- workflow template
- memory backend
- checkpointer
- UI panel
- observability sink

### 4.11 多轮对话、持续任务推进与中断恢复

必须把"对话"和"任务"分开建模，

- 对话线程，消息流
- 任务流，目标、计划、步骤、状态、依赖

中断恢复建议，

- 每个 thread 有当前 checkpoint pointer
- 每个 workflow step 有 last stable snapshot
- 每个 child agent 有 resumable mailbox/taskboard state
- 人工介入不是异常，而是正常状态，`interrupted_waiting_human`

### 4.12 推荐的分层实现顺序与 MVP 范围

在 v1 的实现顺序基础上，结合 §3.7 的 harness 洞察和讨论记录中的 MVP 建议，这里细化分阶段路线。

#### 4.12.1 MVP 范围，v1，核心 9 件套，

第一版 Agent Core 只做这些，

1. **AgentSpec**，Agent 定义与生命周期
2. **ToolSpec**，工具定义、注册、schema
3. **WorkflowSpec**，工作流定义、节点、边
4. **Workflow Engine**，基础图执行引擎，线性、DAG、条件，
5. **Context Manager**，上下文组装、token 预算
6. **Memory Store**，至少 supporting working + episodic
7. **Model Gateway**，多 Provider 统一接口
8. **State / Event / Trace**，持久化、事件日志、追踪
9. **CLI 或简单 Web UI**，最小交互面

**MVP 验证目标，**

> 能稳定运行一个复杂工作流，支持子 Agent、工具调用、上下文压缩、记忆写入、失败恢复。

**MVP 先不做，**

- 太复杂的 UI，可视化编排器，
- 太复杂的 marketplace，插件市场，
- 太复杂的多租户，企业级隔离，
- 太复杂的权限系统，RBAC / ABAC，
- 太复杂的自动优化，planner 自优化，
- 太复杂的 Agent 社会结构，去中心化协商，

> 原因参见 §3.7 的 98.4% harness 洞察，harness 的质量比功能数量更关键。先把 harness 做扎实，事件系统、状态管理、恢复机制，，再扩展上层功能。

#### 4.12.2 分阶段建设路线

如果从零开始实现，建议按以下顺序建设，

**Phase 1 - 内核，harness 的基础，，**

1. `kernel.runtime`，事件循环、状态快照、checkpoint、Event Sourcing 基础设施
2. `state.persistence`，统一快照、恢复、事件回放
3. `workflow.graph`，节点、路由、子图、恢复
4. `tool.runtime`，tool、workbench、adapter、result envelope

**Phase 2 - 智能层，**

5. `agent.runtime`，spawn、await、mailbox、taskboard
6. `memory.system`，working + episodic + artifact，先不做复杂 semantic
7. `context.manager`，上下文组装、压缩、token 预算控制

**Phase 3 - 生态层，**

8. `extension.sdk`，manifest + 插件加载
9. `observability.governance`，trace、cost、context ledger

**Phase 4 - 增强层，**

10. semantic / procedural memory 与高级 planner
11. Pipeline / Blackboard 协作模式
12. 可视化编排器 / 高级 UI

### 4.13 最终建议，哪些项目最值得作为新框架的直接蓝本

如果目标是“高度抽象、模块化、灵活、易扩展”的 Agent 框架，最值得吸收的组合是，

- **工作流内核**，`LangGraph`
- **事件驱动/多代理 runtime 思想**，`AutoGen Core`
- **高层多代理编程模型与统一记忆**，`CrewAI`
- **子代理任务运行时**，`codeg`
- **分层记忆模型**，`GenericAgent`
- **上下文治理/项目记忆产品化能力**，`desktop-cc-gui`
- **扩展 manifest 与权限系统**，`AionUi`

最不建议直接照搬的对象，

- `ClaudeCodeRev`，恢复源码，不适合作为基座
- `SpectrAI`，有不少值得学的局部设计，但当前代码实现与 README 规划存在落差

---

## 5. 总结

这 10 个项目可以分成三大类，

1. **真正的框架内核型**，`LangGraph`、`AutoGen Core`、`CrewAI`
2. **高价值运行时/产品样本型**，`codeg`、`desktop-cc-gui`、`AionUi`、`SpectrAI`、`ClaudeCodeRev`
3. **理念/机制创新型**，`GenericAgent`

如果只选一个作为基座，`LangGraph` 最强。
如果要做“能落地的 Agent 框架产品”，不能只学 `LangGraph`，还必须把，

- `codeg` 的子代理 runtime
- `CrewAI` 的统一记忆
- `AionUi` 的扩展声明
- `desktop-cc-gui` 的上下文治理

一起吸收进来。

**最优方案不是复制某个项目，而是做分层组合，**

- 内核学 `LangGraph`
- 多代理学 `AutoGen/Core + codeg`
- 记忆学 `CrewAI + GenericAgent`
- 扩展学 `AionUi`
- 产品治理学 `desktop-cc-gui`

### 最终架构愿景

综合 v1 的项目分析和本版收敛出的设计原则，最终要设计的不是一个“集合了所有功能的 Agent 工具”，而是一个，

> **以 Workflow Graph 为编排核心，以 Agent / Tool / SubWorkflow / Human 作为统一可递归组合的 Node，以 Memory + Context Manager，严格分离，作为智能输入管理层，以 Tool Runtime，含 Agent/CLI/Workflow 三种 Adapter，+ Policy + Sandbox 作为安全执行层，以 Event Store，Event Sourcing，+ State Store + Trace 作为可观测与可恢复底座，以 Manifest Plugin System 保证长期扩展性的 Agent Kernel。**

**10 条架构原则作为设计的终极约束，§3.6.1，，**

1. 一切都是 Node
2. 一切执行都产生 Event
3. Workflow 是 Graph，不只是 Chain
4. Agent 是可组合单元，不是固定类
5. Tool 是统一外部能力接口
6. Memory 和 Context 分离
7. 状态必须持久化
8. 默认安全
9. 可观察性是核心能力
10. 先做小核心，再做生态
