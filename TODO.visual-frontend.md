# Meadow Visual Frontend TODO

更新时间: 2026-06-08

目标: 将桌面可视化从手写 DOM 迁移为 `Tauri + Vite + React + TypeScript` 模块化工程，保持 API-first，逐步覆盖日常对话、节点流转、配置中心、Skill、审批、MCP、任务、控制台、事件流、工作流和子工作流审核。

## Phase V0 - 架构与工程骨架

- [x] 输出宏观架构设计文档: `docs/visual-frontend-architecture.md`。
- [x] 新建独立前端 TODO: `TODO.visual-frontend.md`。
- [x] 新建 `desktop/ui` React/Vite/TypeScript 工程。
- [x] 调整 Tauri `frontendDist` 与 `devUrl` 指向 Vite 工程。
- [x] 建立 `shared/api`、`shared/ui`、`shared/layout`、`features/*` 目录边界。
- [ ] 保留 `desktop/static` 作为历史参考，后续确认可移除时再删除。
- [x] 完成首版构建验证。

## Phase V1 - 设计系统与 Shell

- [x] 建立设计 token: 颜色、状态色、间距、圆角、字体、阴影。
- [x] 实现基础组件首版: Button、Badge、Card、Panel、Field、StatusDot、EmptyState。
- [x] 实现布局组件首版: AppShell、Sidebar、Topbar。
- [x] 实现全局 API URL 配置与连接状态显示。
- [x] 实现统一 `ActionSpec` 类型，覆盖 view/create/update/delete/stop/retry/approve/reject。

## Phase V2 - 日常对话

- [x] 迁移对话 session 列表、消息流、输入区首版。
- [x] 支持发送、暂停、重试、清空上下文首版；停止/取消 run 后续接入统一 ActionSpec。
- [ ] 支持显示 run_id、工具调用、等待审批、子 Agent 摘要。
- [ ] 支持右侧上下文检查器: Skill、MCP、工作区、审批、Agent/Workflow。
- [ ] 支持后续模型选择、附件、图片输入、推理展示的 UI 插槽。

## Phase V3 - 配置中心

- [x] 迁移大模型 Provider/Model/Capability/Agent Binding 只读卡片首版。
- [x] 迁移 Agent 默认策略只读预览首版。
- [ ] 迁移 UI/MCP/Control 简单配置。
- [ ] 保留高级 JSON 编辑兜底。
- [ ] 增加密钥字段写回保护提示与 provider 健康检查入口。

## Phase V4 - Skill、MCP、审批、任务

- [ ] 迁移 Skill 管理: 创建、编辑、启用、弃用、导入/导出。
- [ ] 迁移 MCP 管理: 新增、编辑、删除、启用、同步、测试连接。
- [ ] 迁移审批中心: 批准、拒绝、限时授权、取消/kill 工具调用。
- [ ] 迁移任务与计划任务: 创建、启停、立即运行、删除、触发历史。

## Phase V5 - 节点、工作流、事件流

- [ ] 实现 Run/Workflow/Tool/Agent/Approval/Artifact 节点列表视图。
- [ ] 支持节点查看、停止、重试、删除、跳过、旁路。
- [ ] 实现子工作流审核视图: 输入、输出、diff、风险、批准/拒绝。
- [ ] 迁移事件流: 游标加载、过滤、复制 payload、跳转对象。
- [ ] 预留 GraphAdapter，后续接入 React Flow 或自研图视图。

## Phase V6 - 多 Agent 工作台

- [ ] 新增 Workbench feature 模块，对接 `/collaboration/workbenches` API。
- [ ] 工作台列表: group chat、CLI 协同、技术评审、parallel delegation 分类与状态。
- [ ] 工作台详情: channel timeline、members、task slices、taskboard items、delegation cards。
- [ ] 操作入口: 创建群聊、创建技术评审、创建并行 delegation、发送消息、生成 decision artifact、取消工作台。
- [ ] 预留真实终端 stream 面板和子 Agent 会话 overlay。
- [ ] 预留 task slice 级 retry/delete/assign/skip/bypass/approve/reject action slots。

## Phase V7 - 控制台与可视化增强

- [ ] 迁移 browser/desktop/mobile target 列表。
- [ ] 展示浏览器页面摘要、截图、UI tree、artifact。
- [ ] 支持动作执行: navigate、inspect、screenshot、dump_ui、click/key/tap/type。
- [ ] 引入轻量动效: 面板切换、展开/收起、运行状态变化。

## Phase V8 - 外部项目 UI Recipe

- [ ] 建立 `docs/visual-recipes/`，沉淀 CodeG/AionUI/其他开源项目的可借鉴 UI 模式。
- [ ] 每个 recipe 记录: 来源、许可、截图/结构、适配到 Meadow 的组件映射、不要照搬的耦合点。
- [ ] 将 recipe 转译到 `shared/ui` 和 feature 模块，而不是直接复制业务代码。

## 验收命令

- [x] `npm --prefix desktop run build`
- [ ] `npm --prefix desktop run dev:web`
- [ ] Tauri dev shell 能加载 React/Vite 页面。
- [ ] Python HTTP host 启动后，日常对话与配置中心能正常调用 API。
