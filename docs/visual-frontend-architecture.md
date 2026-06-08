# Meadow Visual Frontend Architecture

更新时间: 2026-06-08

## 目标

Meadow 桌面端从手写 DOM 迁移为模块化前端工程。前端只负责可视化、交互编排和 API 调用，不承载 Agent Runtime、Policy、MCP、Control、Workflow 执行逻辑。

核心目标:

- 对齐 CodeG/AionUI 的工作台式体验: 会话优先、左侧导航、右侧检查器、紧凑卡片、弹层/抽屉、状态徽标、运行过程可视化。
- 支持复杂操作面: 查看、流转、删除、停止、重试、修改、审批、旁路、审核、恢复。
- 支持快速吸收外部开源项目的 UI 模式: 通过本项目自己的设计系统和 feature 模块复用布局、卡片、切换、动画，而不是复制对方业务耦合。
- 保持 API-first: 后端能力通过 HTTP/SSE/WebSocket/本地 sidecar 适配接入，前端不直接调用 Python 内部对象。

## 技术选型

采用 `Tauri + Vite + React + TypeScript`。

选择原因:

- Vite 比 Next.js 更适合桌面壳: 构建简单、dev server 快、不引入 SSR/路由服务端复杂度。
- React 生态更方便借鉴 CodeG/AionUI 的组件化布局、设置页、对话页、selector、overlay、dialog。
- TypeScript 能把 API DTO、运行状态、操作权限、模块边界表达清楚。
- 后续可引入 TanStack Query、Zustand/Jotai、Radix/shadcn、Framer Motion，但首版只搭建轻依赖骨架，避免过早框架锁死。

## 总体分层

```text
desktop/ui
  app/                  # 应用装配、路由、全局 providers
  features/             # 业务功能模块
  shared/api/           # Meadow HTTP/SSE API client 和 DTO
  shared/ui/            # 基础 UI 组件与设计系统
  shared/layout/        # Shell、Sidebar、Topbar、SplitPane、Inspector
  shared/state/         # 本地 UI 状态，不保存后端事实
  shared/lib/           # 纯工具函数
```

运行链路:

```text
React UI -> shared/api -> Meadow HTTP/SSE API -> Python Agent Kernel
Tauri shell -> loads Vite dev server or dist
```

## 模块边界

每个 `features/*` 模块遵循相同结构:

```text
features/<name>/
  api.ts                # 仅封装该模块 API 调用
  model.ts              # DTO 到 ViewModel 的纯转换
  components/           # 模块私有组件
  pages/                # 路由级页面
  index.ts              # 对外导出稳定入口
```

约束:

- Feature 之间不直接互相 import 私有组件。
- 跨模块通信通过 URL state、全局 shell state、API 事实、事件流或明确的 app service。
- 所有副作用操作必须走 API client，UI 组件只接收 command handler。
- 删除、停止、重试、修改等危险操作必须走统一 `ActionSpec` 和确认/审批模式。

## 功能域

### 日常对话

用途: 默认入口，持续沟通，并由模型自主组合 Skill、Agent、MCP、Workflow、Control。

首屏结构:

- 左侧: 对话列表、搜索、新建。
- 中间: 消息流、运行状态、输入区、附件/Skill/模型选择。
- 右侧: 当前上下文、启用 Skill、MCP、工作区、待审批、子 Agent。

关键交互:

- 发送、暂停、停止、重试、清空上下文。
- 展示工具调用、推理摘要、审批等待、子任务状态。
- 支持从消息跳转到 run、tool call、artifact、workflow step。

### 节点与流转

用途: 查看 Runtime/Workflow/Agent/Task 的执行图和流转状态。

对象:

- Run node、workflow node、tool call node、agent delegation node、human approval node、artifact node。

操作:

- 查看详情、停止、重试、删除、跳过、旁路、恢复。
- 子工作流审核: 查看输入/输出/diff/风险，批准或拒绝进入下一步。

视觉:

- 首版用列表/分组/状态卡片。
- 后续可接入 React Flow 或自研 Canvas 图视图，但必须通过 `GraphAdapter` 隔离。

### 配置中心

用途: 热更新 UI、大模型、Agent、MCP、Control、权限策略。

模块:

- Model Providers: Provider、Model、Capability、Agent Binding。
- Agent Defaults: 默认 Agent、委派策略、模型兜底。
- MCP: Server、transport、agent binding、启停。
- Control: browser/desktop/mobile 后端、健康状态。
- UI: 语言、主题、密度。
- Advanced JSON: 兜底编辑入口。

### Skill 管理

用途: 统一管理解释型 Skill、编译型 workflow skill、内置 atomic skill。

操作:

- 创建、编辑、启用、弃用、复制、导入/导出。
- 关联推荐工具、MCP、Workflow、Agent。
- 查看使用记录与失败模式。

### 审批中心

用途: 管理所有高风险动作。

对象:

- 文件/命令/网络/浏览器/桌面/移动/MCP/子工作流审核。

操作:

- 批准、拒绝、一次性授权、限时授权、取消工具调用、终止运行。

### MCP

用途: 管理 MCP Server、工具列表、Agent 绑定和健康状态。

操作:

- 新增、编辑、删除、启用、同步、测试连接。

### 任务与计划任务

用途: 查看任务、计划任务、触发历史、重试策略。

操作:

- 创建、启停、立即运行、删除、查看触发 run。

### 控制台

用途: 浏览器/桌面/移动控制可视化。

结构:

- Target 列表、截图/页面摘要/UI tree、动作执行、artifact 入口。

### 事件流

用途: 按 run、task、agent、tool、global 查看 runtime event。

交互:

- 游标加载、实时订阅、过滤、复制 payload、跳转对象。

## 操作模型

所有用户操作归一为 `ActionSpec`:

```ts
type ActionSpec = {
  id: string
  label: string
  intent: "view" | "create" | "update" | "delete" | "stop" | "retry" | "approve" | "reject"
  danger?: boolean
  requiresConfirm?: boolean
  run: () => Promise<void>
}
```

这样删除、停止、重试、审批等动作可以共享:

- 按钮状态
- 确认弹窗
- 错误提示
- 乐观刷新
- 审计事件跳转

## 数据获取

首版使用轻量 `fetchJson` 和 `useAsyncResource`。后续升级为 TanStack Query 时，Feature API 不需要重写。

数据原则:

- 后端事实来自 API，不在前端重复建事实存储。
- 前端只保存 UI state: 当前视图、筛选、选中对象、展开状态、草稿。
- SSE/WebSocket 只作为刷新信号和实时事件源，不替代 API 查询。

## 设计系统

首版内置:

- Button、IconButton、Badge、Card、Panel、Tabs、Dialog、Toolbar、Field、Select、Textarea、StatusDot、EmptyState。
- Layout: AppShell、Sidebar、Topbar、WorkspaceGrid、InspectorPanel。
- Tokens: 颜色、间距、圆角、阴影、字体、状态色。

后续可以替换底层实现，但业务模块只使用 `shared/ui`。

## 迁移策略

阶段 1:

- 保留 `desktop/static` 作为历史参考。
- 新建 `desktop/ui`，Tauri 指向 Vite dev server 和 `ui/dist`。
- 实现新的 AppShell、导航、配置中心、日常对话占位/基础 API 调用。

阶段 2:

- 迁移 Skill、Workspace、Approvals、MCP、Schedules、Control、Events。
- 引入统一 ActionSpec、Dialog、Toast。

阶段 3:

- 增加节点流转视图、工作流图、子工作流审核、旁路逻辑可视化。
- 接入运行事件实时订阅。

阶段 4:

- 建立外部项目 UI recipe 库: 记录 CodeG/AionUI/其他项目中可借鉴的布局模式、组件行为、动画，并转译到 Meadow `shared/ui`。

## 非目标

- 不把 CodeG/AionUI 的运行时结构硬融合进 Meadow。
- 不让前端直接执行 Agent/Tool/Workflow。
- 不在首版引入复杂图编辑器或重动画依赖。
