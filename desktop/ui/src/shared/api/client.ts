import type {
  ApiEnvelope,
  Approval,
  ChatMessage,
  ChatSession,
  ConfigSection,
  ControlTarget,
  McpServer,
  RuntimeEvent,
  ScheduledTask,
  SkillCard,
  ToolCallRecord,
  Workspace,
} from "./types";

const DEFAULT_API_URL = import.meta.env.VITE_MEADOW_API_URL || "http://127.0.0.1:8080";
const API_URL_KEY = "meadow.apiUrl";

export function getStoredApiUrl(): string {
  return localStorage.getItem(API_URL_KEY) || DEFAULT_API_URL;
}

export function storeApiUrl(url: string): string {
  const normalized = normalizeApiUrl(url);
  localStorage.setItem(API_URL_KEY, normalized);
  return normalized;
}

export function normalizeApiUrl(url: string): string {
  return (url || DEFAULT_API_URL).trim().replace(/\/+$/, "");
}

export class MeadowApiClient {
  constructor(private readonly baseUrl: string) {}

  get apiUrl(): string {
    return this.baseUrl;
  }

  async getChatSessions(): Promise<ChatSession[]> {
    return (await this.get<{ chat_sessions: ChatSession[] }>("/chat/sessions")).chat_sessions || [];
  }

  async createChatSession(title = "新的对话"): Promise<ChatSession> {
    return (await this.post<{ chat_session: ChatSession }>("/chat/sessions", { title })).chat_session;
  }

  async getChatMessages(sessionId: string): Promise<ChatMessage[]> {
    return (await this.get<{ messages: ChatMessage[] }>(`/chat/sessions/${encodeURIComponent(sessionId)}/messages`)).messages || [];
  }

  async sendChatMessage(sessionId: string, content: string, runId?: string): Promise<ChatMessage[]> {
    return (
      await this.post<{ messages: ChatMessage[] }>(`/chat/sessions/${encodeURIComponent(sessionId)}/messages`, {
        content,
        mode: "agent",
        run_id: runId,
      })
    ).messages || [];
  }

  async pauseChat(sessionId: string): Promise<void> {
    await this.post(`/chat/sessions/${encodeURIComponent(sessionId)}/pause`, { reason: "用户在可视化工作台暂停" });
  }

  async retryChat(sessionId: string): Promise<void> {
    await this.post(`/chat/sessions/${encodeURIComponent(sessionId)}/retry`, {});
  }

  async clearChat(sessionId: string): Promise<void> {
    await this.delete(`/chat/sessions/${encodeURIComponent(sessionId)}/messages`);
  }

  async getConfig(section: string): Promise<ConfigSection> {
    return (await this.get<{ config_section: ConfigSection }>(`/config/${encodeURIComponent(section)}`)).config_section;
  }

  async listSkills(): Promise<SkillCard[]> {
    return (await this.get<{ skills: SkillCard[] }>("/skills")).skills || [];
  }

  async listApprovals(): Promise<Approval[]> {
    return (await this.get<{ approvals: Approval[] }>("/approvals")).approvals || [];
  }

  async listWorkspaces(): Promise<Workspace[]> {
    return (await this.get<{ workspaces: Workspace[] }>("/workspaces")).workspaces || [];
  }

  async listMcpServers(): Promise<McpServer[]> {
    return (await this.get<{ mcp_servers: McpServer[] }>("/mcp-servers")).mcp_servers || [];
  }

  async listScheduledTasks(): Promise<ScheduledTask[]> {
    return (await this.get<{ scheduled_tasks: ScheduledTask[] }>("/scheduled-tasks")).scheduled_tasks || [];
  }

  async listControlTargets(): Promise<ControlTarget[]> {
    return (await this.get<{ targets: ControlTarget[] }>("/control/targets")).targets || [];
  }

  async listEvents(): Promise<RuntimeEvent[]> {
    const response = await fetch(`${this.baseUrl}/events`);
    if (!response.ok) throw new Error(`GET /events failed: ${response.status}`);
    const text = await response.text();
    return text
      .split("\n")
      .filter(Boolean)
      .map((line) => JSON.parse(line) as RuntimeEvent);
  }

  async listEventsByRun(runId: string): Promise<RuntimeEvent[]> {
    const response = await fetch(`${this.baseUrl}/events?run_id=${encodeURIComponent(runId)}`);
    if (!response.ok) throw new Error(`GET /events failed: ${response.status}`);
    const text = await response.text();
    return text
      .split("\n")
      .filter(Boolean)
      .map((line) => JSON.parse(line) as RuntimeEvent);
  }

  async listToolCalls(runId?: string): Promise<ToolCallRecord[]> {
    const suffix = runId ? `?run_id=${encodeURIComponent(runId)}` : "";
    return (await this.get<{ tool_calls: ToolCallRecord[] }>(`/tool-calls${suffix}`)).tool_calls || [];
  }

  private async get<T>(path: string): Promise<T> {
    return this.json<T>("GET", path);
  }

  private async post<T = unknown>(path: string, body: unknown): Promise<T> {
    return this.json<T>("POST", path, body);
  }

  private async delete<T = unknown>(path: string): Promise<T> {
    return this.json<T>("DELETE", path);
  }

  private async json<T>(method: string, path: string, body?: unknown): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = (await response.json()) as ApiEnvelope<T>;
    if (!response.ok || data.ok === false) {
      throw new Error(data.error || `${method} ${path} failed`);
    }
    return data as T;
  }
}
