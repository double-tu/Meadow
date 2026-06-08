# Collaboration Workbench Architecture

更新时间: 2026-06-08 CST

## 目标

工作台模块承载 Meadow 的多 Agent 协作形态:

- 群聊: human、moderator、worker、reviewer 在同一 channel 内持续讨论、选人发言、沉淀 decision artifact。
- 多 CLI 协同: Codex/Claude/Gemini/其他 CLI Agent 通过 connector 作为参与者加入工作台，每个 CLI 会话可执行任务、返回 artifact、被取消。
- 技术评审: 将一个目标拆成 architecture、correctness、security、tests 等评审切片，支持 reviewer 子 Agent 并行执行和后续人工审核。
- 并行子 Agent 分发: 父 Agent 可把搜索、网页打开、资料提取等任务拆给多个子 Agent 并发执行，再汇总结果。

这个模块不复制 CodeG 或 AionUI 的运行时结构。CodeG 可借鉴的是 delegation binding、child session 卡片、task context、终端 tab 的状态管理；AionUI 可借鉴的是 conversation、assistant、skill、channel、scheduled task 的统一入口。Meadow 的实现必须落在自己的 Kernel 边界内。

## 分层

```text
Desktop / Daily Chat / API Client
  -> CollaborationWorkbenchService
  -> InteractionFabric / GroupChatService / TaskBoardService
  -> AgentDelegationBroker / AgentConnector / CapabilityRuntime / Workflow Runtime
  -> EventStore / InteractionStore / ArtifactStore
```

职责边界:

- `CollaborationWorkbenchService`: 只负责装配协作对象、任务切片、成员、channel、delegation 请求和工作台事件。
- `InteractionFabric`: 保存 channel、participant、message。
- `GroupChatService`: 维护发言策略和 discussion turn。
- `TaskBoardService`: 保存可视化任务条目。
- `AgentDelegationBroker`: 真正启动、查询、取消外部/CLI 子 Agent。
- UI: 展示工作台、消息、任务切片、delegation 状态和审批，不在前端判断任务如何拆分。

## 领域模型

- `CollaborationWorkbench`: 顶层工作台，包含 kind、status、parent_run_id、channel_id、group_chat_id、member_ids、task_slice_ids、delegation_task_ids。
- `WorkbenchMember`: 工作台成员，可是 human、agent、remote_agent、observer；可绑定 `agent_session_id`、`connector_id`、`agent_type`。
- `WorkbenchTaskSlice`: 可并发执行或评审的任务切片，可绑定 taskboard item 和 delegation task。

工作台运行事件:

- `workbench.created`
- `workbench.updated`
- `workbench.cancelled`

## API

首版 HTTP API:

- `GET /collaboration/workbenches`
- `GET /collaboration/workbenches/{workbench_id}`
- `POST /collaboration/workbenches/group-chat`
- `POST /collaboration/workbenches/cli`
- `POST /collaboration/workbenches/technical-review`
- `POST /collaboration/workbenches/parallel-delegation`
- `POST /collaboration/workbenches/{workbench_id}/messages`
- `POST /collaboration/workbenches/{workbench_id}/decision`
- `POST /collaboration/workbenches/{workbench_id}/cancel`

`auto_start=false` 时只创建成员、channel、task slice、taskboard item；`auto_start=true` 时要求 HTTP host 已通过 `agent_connectors` 配置装配 delegation broker，并会立即启动子 Agent。

## 与日常对话的关系

日常 Agent 不应硬编码“搜索/评审/群聊”关键词。模型应该通过上下文中的 Skill 描述、能力目录和工作台 API schema 自主决定:

- 创建群聊工作台。
- 创建技术评审工作台。
- 创建并行 delegation 工作台。
- 查询工作台和 delegation 状态。
- 给工作台 channel 发送消息。
- 取消工作台或子任务。

工作台 API 让 Daily Agent 可以把复杂任务从单条对话扩展成可观察、可取消、可审计的协作空间。

## 模型可见工具与挂起/推进

工作台能力通过 `OrchestrationCapabilityProvider` 进入 `ContinuousAgentRunner` 的工具目录，而不是由 UI 或 ChatService 做意图关键词匹配。

模型可用工具:

- `workbench_create`: 创建 group_chat、cli_collaboration、technical_review、parallel_delegation。
- `workbench_status`: 查询工作台、消息、成员、task slice、delegation 状态。
- `workbench_message`: 代表主持人、用户代理、reviewer 或其他参与者向工作台 channel 发送消息。
- `workbench_decision`: 从群聊工作台生成 decision artifact。
- `workbench_cancel`: 取消工作台及其子 delegation。

异步/挂起语义:

- 子 Agent、CLI 和持续任务通过 workbench/delegation 状态异步推进，不阻塞用户继续日常对话。
- 当模型缺少 connector、成员、评审目标或用户授权时，使用 `user_input_request` 挂起当前工具循环，等待用户补充。
- 用户后续可以继续在日常对话里引用同一个 `workbench_id`，模型可调用 `workbench_status` 或 `workbench_message` 继续推进。
- 人类也可以直接进入工作台 channel 参与，日常 Agent 只是在需要时充当主持人/代理人，而不是唯一入口。

## 后续扩展

- 为工作台补 `ActionSpec` 映射: stop、retry、delete、assign、approve、reject、bypass。
- 工作台前端页: 左侧工作台列表，中间 channel timeline / task slice board，右侧 member/delegation/approval inspector。
- 子 Agent 结果汇总器: 将多个 delegation report 和 artifact refs 归并成决策 artifact。
- 真实终端视图: 对接 CLI connector/terminal stream，使 UI 能查看每个 CLI 会话输出和退出状态。
- 技术评审策略: 支持 diff/patch artifact、review record、merge queue、冲突修复 workflow。
- 大规模搜索调度: 支持批量 URL 分片、预算、速率限制、失败重试、结果去重和 artifact 化。
