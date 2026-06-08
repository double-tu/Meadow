# Daily Agent Conversation Flow

更新时间: 2026-06-07 CST

## 目标

日常对话界面是 Meadow 的默认 Agent 入口。它需要对齐 GenericAgent 的核心体验: 用户可以自然对话, 也可以在同一个对话里让 Agent 搜索网页、控制浏览器/桌面/移动端、读写文件、执行代码、调用 MCP、运行 Workflow、委派子代理、并发分配任务、暂停/重试/清空上下文。

但 Meadow 不复制 GenericAgent 的运行时结构。GenericAgent 的精华是“模型在循环中自主选择工具并根据真实结果继续推理”; Meadow 需要把这个流程融入现有 Kernel 架构:

```text
Host / Desktop Chat
  -> Conversation & Task Hub
  -> Default Daily Agent Workflow
  -> Agent Runtime Loop
  -> Model Gateway
  -> CapabilityRuntime / Workflow Runtime / Agent Delegation / MCP / Workbench
  -> RuntimeEvent / ToolCallRecord / ArtifactRef / Memory
  -> ContextManager
  -> Agent Runtime Loop continues or finishes
  -> Desktop UI renders messages, tool calls, task status, approvals, artifacts, events
```

## 非目标

- 不在 UI 内判断意图。
- 不在 `DesktopChatService` 内硬编码天气、搜索、浏览器、任务拆分等关键词路由。
- 不让 Skill 直接绕过 Runtime/Policy 执行副作用。
- 不把外部 Agent 降级为一次性普通工具; 底层仍保留 AgentConnector 的 session、status、artifact、cancel、stream/progress 语义。

## 核心边界

### Desktop Chat

职责:

- 接收用户消息。
- 展示对话、任务、工具调用、审批、事件和 artifact。
- 发送 pause/retry/clear/resume 等用户控制。

不负责:

- 选择工具。
- 直接调用模型。
- 直接执行浏览器、文件、代码、MCP、Workflow、子 Agent。

### Conversation & Task Hub

职责:

- 写入 Thread/Turn。
- 建立 Objective/Task。
- 创建或继续默认 Daily Agent Run。
- 维护 Thread、Task、Run、Message 的关联。
- 将用户控制转换为 Runtime control、human intervention、tool-call cancel/kill 等应用服务调用。

不负责:

- 执行节点。
- 直接调用模型或工具。
- 写长期记忆。

### Default Daily Agent Workflow

日常对话不是单次 chat completion, 而是一个内建 Agent Workflow:

```text
load_session
  -> retrieve_memory
  -> select_skills_and_capabilities
  -> build_context
  -> call_model
  -> route_action
  -> execute_capability_or_workflow_or_agent
  -> persist_turn_and_events
  -> update_working_memory
  -> continue_or_finish
```

这个 Workflow 必须可恢复、可审计、可暂停、可取消, 并把副作用交给 `CapabilityRuntime`、`RuntimeEngine`、`AgentDelegationBroker` 或 MCP/Workbench adapter。

### Agent Runtime

职责:

- 根据 Context/Memory/Skills/Capability catalog 调用模型。
- 解析模型行动意图。
- 将行动落成:
  - `ToolCallRequest`
  - `ExecutionCommand`
  - `PlanPatch`
  - `WorkflowAdapter` call
  - `AgentDelegation` request
  - `HumanIntervention` / user input request
- 接收真实结果并继续循环。

Agent Runtime 不能直接绕过策略执行副作用。

### Skill

Skill 是 Agent 可解释的程序性知识, 不是执行器。

Skill 负责告诉模型:

- 什么时候使用。
- 怎么使用。
- 推荐哪些工具、Workflow、Agent 类型或 MCP 能力。
- 有哪些约束和常见失败。

Skill 可以引用:

- atomic capabilities
- workflow ids
- mcp tool names
- agent connector roles
- procedural memory refs
- examples / artifacts

Skill 不能直接修改 workflow graph; 如需沉淀可复用流程, 应通过 autonomy/workflow induction 生成 draft `WorkflowSpec`。

### Capability / Workflow / Agent / MCP Catalog

模型需要通过 Context 看到一份经过裁剪的能力目录:

- Atomic capabilities: 文件、HTTP、浏览器、桌面、移动、代码执行、记忆埋点、用户输入。
- Workflow catalog: 已注册 workflow/template 及其适用条件、输入输出。
- Agent delegation catalog: 可用 connector、agent type、委派/状态/取消语义。
- MCP catalog: 已启用 MCP server 和 tool 描述。
- Workbench/control catalog: browser/desktop/mobile target 和可用动作。
- Policy context: 当前 grant、审批要求、风险边界。

能力目录必须由 ContextManager/ToolVisibility 策略裁剪, 不默认把所有 schema 全塞给模型。

## 标准执行流

### 普通对话

```text
User Turn
  -> Hub writes Thread/Turn
  -> Daily Agent Run
  -> Context includes recent conversation + active skills + relevant memory
  -> Model returns final response
  -> Assistant Turn persisted
```

### 网页/浏览器任务

```text
User: 搜索今天的天气
  -> Context includes Web/Browse Skill + http/browser capabilities
  -> Model selects Skill and emits tool call
  -> CapabilityRuntime policy check
  -> HTTP/browser adapter executes
  -> ToolResult + ToolCallRecord + Audit/Event
  -> Context receives tool result summary/artifact ref
  -> Model answers from real result
```

### 工作流任务

```text
User: 按代码评审流程检查这个仓库
  -> Context includes Workflow catalog and matching Skill
  -> Model selects workflow_run / ExecutionCommand
  -> RuntimeEngine creates subworkflow run
  -> Parent agent awaits/observes result
  -> Artifacts and decision returned to conversation
```

### 子代理与并发任务

```text
User: 拆成前端/后端/测试三个子任务并发执行
  -> Context includes Agent delegation catalog and TaskBoard/AgentPool state
  -> Model creates task split plan
  -> CollaborationWorkbenchService creates a parallel-delegation workbench
  -> AgentDelegationBroker starts child tasks or TaskBoard claims
  -> Child agents run independently with lineage, status, cancel, artifact handoff
  -> Parent agent polls/waits and summarizes structured outputs
```

### 群聊 / 多 CLI / 技术评审工作台

```text
User: 组织几个子 Agent 做技术评审
  -> Context includes Workbench Skill + Collaboration Workbench API schema
  -> Model selects workbench kind: group_chat / cli_collaboration / technical_review / parallel_delegation
  -> CollaborationWorkbenchService creates members, channel, task slices, taskboard items
  -> Optional auto_start delegates slices to configured CLI/remote agent connectors
  -> UI renders channel timeline, task slices, delegation cards, approvals, artifacts
  -> Model or user can send messages, create decision artifacts, cancel/retry slices
  -> If missing inputs/authorization, model calls user_input_request and the chat turn waits for the user
```

详见 `docs/collaboration-workbench-architecture.md`。工作台是日常对话扩展到多 Agent 协作空间的应用层入口，不改变 Runtime/Capability/Policy 的职责边界。

日常 Agent 可见的工作台工具包括 `workbench_create`、`workbench_status`、`workbench_message`、`workbench_decision`、`workbench_cancel`。模型可以在多轮工具循环里先创建工作台，再根据真实 `workbench_id` 继续发送主持人消息或查询状态。

### 用户输入/审批/暂停

```text
Model needs missing info
  -> user_input_request / HumanIntervention
  -> Run enters waiting_input/interrupted state
  -> Desktop UI shows prompt
  -> User reply resumes run
```

高风险副作用:

```text
Tool call requires approval
  -> PolicyEngine returns require_approval
  -> ApprovalRequest persisted
  -> UI shows structured preview
  -> approve/reject resumes or fails current step
```

## 实现阶段

### P0: 固化流转和约束

- 记录本文档。
- 更新 TODO, 明确桌面日常对话必须走 Conversation & Task Hub + Default Daily Agent Workflow。
- 移除或收敛 chat service 内的旁路 agent loop。

### P1: Hub MVP

- 新增 `ConversationTaskHub` 应用服务。
- 将 desktop chat session/message 映射到 Thread/Turn 概念。
- 用户消息创建 Task/Run 关联。
- pause/retry/clear 映射到 run/task/message 控制。

### P2: Daily Agent Loop MVP

- 在 Agent Runtime 增强多轮 tool loop。
- 模型输出归一化为 action:
  - final response
  - capability call
  - workflow call
  - agent delegation
  - user input request
- 工具结果回灌 Context, 直到 finish 或达到上限。

### P3: Capability Catalog

- 内置原子能力以 Skill + capability schema 进入上下文。
- Workflow catalog、Agent delegation catalog、MCP catalog、Workbench catalog 进入上下文。
- 接入 tool visibility pruning 和 policy context。

### P4: Desktop UI 对齐

- 日常对话展示 run/task 状态。
- 展示 tool calls、审批、artifact、子代理 delegation card、workflow progress。
- pause/retry/clear/resume 操作对应真实 runtime 控制。

## 验收标准

- 在日常对话中, “搜索今天的天气”会由模型选择 Web/Browse Skill 并执行真实 HTTP 或 browser capability, 不是文本说明。
- 在日常对话中, “拆给多个子代理并发执行”会创建可追踪的 child delegation/task, 并能查看状态和取消。
- 在日常对话中, “运行某个 workflow”会创建 Runtime run, 并能通过事件流观察。
- 所有副作用能力都有 ToolCallRecord、PolicyDecision、Audit/Event。
- UI 只消费 API/事件, 不包含模型意图判断逻辑。
