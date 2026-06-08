import { Bot, Brain, Image, KeyRound, Server, Settings2 } from "lucide-react";
import { useMemo } from "react";

import type { MeadowApiClient } from "../../shared/api/client";
import type { AgentBinding, LlmProvider } from "../../shared/api/types";
import { useAsyncResource } from "../../shared/lib/useAsyncResource";
import { Badge, Card, EmptyState, Panel, StatusDot } from "../../shared/ui";

type ConfigPageProps = {
  api: MeadowApiClient;
};

type LlmConfigView = {
  providers: LlmProvider[];
  agent_bindings: AgentBinding[];
  active_provider_id?: string;
  active_model_id?: string;
};

export function ConfigPage({ api }: ConfigPageProps) {
  const llm = useAsyncResource(async () => api.getConfig("llm"), [api]);
  const agents = useAsyncResource(async () => api.getConfig("agents"), [api]);
  const config = useMemo(() => normalizeLlmConfig(llm.data?.data), [llm.data]);

  if (llm.error) return <EmptyState title="配置加载失败" detail={llm.error} />;

  return (
    <div className="page-grid">
      <Panel title="模型接入">
        <div className="card-grid two">
          {config.providers.map((provider) => (
            <Card
              key={provider.provider_id}
              title={provider.name || provider.provider_id}
              meta={
                <div className="badge-row">
                  <Badge tone="blue">{provider.kind}</Badge>
                  <Badge tone={provider.enabled === false ? "red" : "green"}>{provider.enabled === false ? "停用" : "启用"}</Badge>
                </div>
              }
            >
              <div className="detail-list">
                <span>
                  <Server size={14} />
                  {provider.base_url || "未配置 Base URL"}
                </span>
                <span>
                  <KeyRound size={14} />
                  {provider.api_key_env || "直接密钥或未配置"}
                </span>
              </div>
              <div className="model-stack">
                {(provider.models || []).map((model) => (
                  <div className="model-card" key={model.model_id}>
                    <div>
                      <StatusDot status={model.enabled === false ? "disabled" : "running"} />
                      <strong>{model.label || model.model_id}</strong>
                      <small>{model.model_id}</small>
                    </div>
                    <CapabilityBadges caps={model.capabilities || {}} />
                  </div>
                ))}
              </div>
            </Card>
          ))}
          {!config.providers.length ? <EmptyState title="暂无 Provider" /> : null}
        </div>
      </Panel>

      <Panel title="Agent 模型绑定">
        <div className="card-grid">
          {config.agent_bindings.map((binding) => (
            <Card
              key={binding.agent_id}
              title={binding.label || binding.agent_id}
              meta={<Badge tone="neutral">{binding.agent_id}</Badge>}
            >
              <div className="binding-line">
                <Bot size={15} />
                <span>{binding.provider_id}</span>
                <strong>{binding.model_id}</strong>
              </div>
              <div className="binding-line">
                <Brain size={15} />
                <span>推理</span>
                <strong>{binding.reasoning_model_id || "跟随主模型"}</strong>
              </div>
              <div className="binding-line">
                <Image size={15} />
                <span>图像</span>
                <strong>{binding.image_model_id || "未指定"}</strong>
              </div>
            </Card>
          ))}
        </div>
      </Panel>

      <Panel title="Agent 策略">
        <pre className="json-preview">{JSON.stringify(agents.data?.data || {}, null, 2)}</pre>
      </Panel>

      <Panel title="后续编辑入口">
        <div className="roadmap-list">
          <span><Settings2 size={15} /> Provider 新增/编辑/删除</span>
          <span><Settings2 size={15} /> 模型能力矩阵编辑</span>
          <span><Settings2 size={15} /> Agent 级模型选择与热更新</span>
          <span><Settings2 size={15} /> MCP、Control、UI 分区表单</span>
        </div>
      </Panel>
    </div>
  );
}

function CapabilityBadges({ caps }: { caps: Record<string, boolean> }) {
  const labels: Record<string, string> = {
    text: "文本",
    function_calling: "工具",
    reasoning: "推理",
    vision: "视觉",
    image_input: "图入",
    image_output: "图出",
    audio_input: "音入",
    audio_output: "音出",
  };
  return (
    <div className="badge-row">
      {Object.entries(labels)
        .filter(([key]) => caps[key])
        .map(([key, label]) => (
          <Badge key={key} tone={key === "reasoning" ? "amber" : "neutral"}>
            {label}
          </Badge>
        ))}
    </div>
  );
}

function normalizeLlmConfig(data: unknown): LlmConfigView {
  if (!data || typeof data !== "object") return { providers: [], agent_bindings: [] };
  const raw = data as Partial<LlmConfigView>;
  return {
    providers: Array.isArray(raw.providers) ? raw.providers : [],
    agent_bindings: Array.isArray(raw.agent_bindings) ? raw.agent_bindings : [],
    active_provider_id: raw.active_provider_id,
    active_model_id: raw.active_model_id,
  };
}
