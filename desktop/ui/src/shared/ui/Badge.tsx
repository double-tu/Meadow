type BadgeProps = {
  children: string | number;
  tone?: "neutral" | "blue" | "green" | "red" | "amber";
};

export function Badge({ children, tone = "neutral" }: BadgeProps) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}
