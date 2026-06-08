import {
  Activity,
  Bot,
  CheckCircle2,
  GitBranch,
  MessageSquareText,
  Network,
  PlaySquare,
  Settings2,
  ShieldCheck,
  SlidersHorizontal,
  TerminalSquare,
  Workflow,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

export type ViewId =
  | "chat"
  | "nodes"
  | "workflow"
  | "config"
  | "skills"
  | "approvals"
  | "mcp"
  | "tasks"
  | "control"
  | "events";

export type NavItem = {
  id: ViewId;
  label: string;
  description: string;
  icon: LucideIcon;
};

export const navigation: NavItem[] = [
  { id: "chat", label: "日常对话", description: "持续沟通与任务执行", icon: MessageSquareText },
  { id: "nodes", label: "节点流转", description: "运行、工具、Agent 节点", icon: GitBranch },
  { id: "workflow", label: "工作流", description: "工作流与子工作流审核", icon: Workflow },
  { id: "config", label: "配置中心", description: "模型、Agent、控制能力", icon: Settings2 },
  { id: "skills", label: "Skill 管理", description: "可复用能力与策略", icon: Bot },
  { id: "approvals", label: "审批", description: "高风险动作与授权", icon: ShieldCheck },
  { id: "mcp", label: "MCP", description: "Server、工具与绑定", icon: Network },
  { id: "tasks", label: "任务", description: "任务、计划与触发历史", icon: PlaySquare },
  { id: "control", label: "控制台", description: "浏览器、桌面、移动", icon: TerminalSquare },
  { id: "events", label: "事件流", description: "Runtime 实时事件", icon: Activity },
];

export const configTabs = [
  { id: "models", label: "模型接入", icon: SlidersHorizontal },
  { id: "agents", label: "Agent 策略", icon: Bot },
  { id: "runtime", label: "运行配置", icon: CheckCircle2 },
] as const;
