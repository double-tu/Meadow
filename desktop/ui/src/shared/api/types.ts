export type ApiEnvelope<T> = T & {
  ok?: boolean;
  error?: string;
};

export type ChatSession = {
  session_id: string;
  title: string;
  status: string;
  message_count: number;
  metadata?: Record<string, unknown>;
};

export type ChatMessage = {
  message_id: string;
  session_id: string;
  role: "user" | "assistant" | "system";
  content: string;
  run_id?: string | null;
  metadata?: Record<string, unknown>;
};

export type ToolCallRecord = {
  tool_call_id?: string;
  run_id?: string;
  capability_id?: string;
  status?: string;
  input?: Record<string, unknown>;
  output?: Record<string, unknown>;
  error?: Record<string, unknown> | string | null;
  created_at?: string;
  updated_at?: string;
  started_at?: string;
  completed_at?: string;
};

export type ConfigSection = {
  section: string;
  data: Record<string, unknown>;
  updated_at: string;
};

export type SkillCard = {
  skill_id: string;
  name: string;
  description?: string;
  when_to_use?: string;
  status: string;
  execution_mode?: string;
};

export type Approval = {
  approval_id: string;
  run_id?: string;
  target_type?: string;
  target_id?: string;
  status: string;
  reason?: string;
};

export type Workspace = {
  workspace_id: string;
  title: string;
  run_ids?: string[];
  artifact_ids?: string[];
  pending_approval_ids?: string[];
  active_tool_call_ids?: string[];
  delegation_task_ids?: string[];
};

export type RuntimeEvent = {
  event_id: string;
  event_type: string;
  run_id?: string | null;
  emitted_at?: string;
  payload?: Record<string, unknown>;
};

export type McpServer = {
  name: string;
  enabled: boolean;
  transport?: { type?: string };
  agent_types?: string[];
};

export type ScheduledTask = {
  task_id: string;
  name: string;
  schedule_kind: string;
  schedule_value: string;
  enabled: boolean;
  next_run_at?: string | null;
  trigger_count?: number;
};

export type ControlTarget = {
  target_id: string;
  kind: string;
  label?: string;
  metadata?: Record<string, unknown>;
};

export type LlmModel = {
  model_id: string;
  label?: string;
  enabled?: boolean;
  context_window?: number;
  capabilities?: Record<string, boolean>;
};

export type LlmProvider = {
  provider_id: string;
  name: string;
  kind: string;
  enabled?: boolean;
  base_url?: string;
  api_key_env?: string;
  timeout_seconds?: number;
  models?: LlmModel[];
};

export type AgentBinding = {
  agent_id: string;
  label?: string;
  provider_id: string;
  model_id: string;
  reasoning_model_id?: string;
  image_model_id?: string;
  show_reasoning?: boolean;
};
