type StatusDotProps = {
  status?: string;
};

export function StatusDot({ status = "unknown" }: StatusDotProps) {
  const normalized = status.toLowerCase();
  const tone = normalized.includes("fail") || normalized.includes("error") ? "red" : normalized.includes("run") ? "green" : "neutral";
  return <span className={`status-dot status-${tone}`} title={status} />;
}
