import type { ButtonHTMLAttributes, ReactNode } from "react";

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  icon?: ReactNode;
  variant?: "default" | "primary" | "ghost" | "danger";
};

export function Button({ children, icon, variant = "default", className = "", ...props }: ButtonProps) {
  return (
    <button className={`button button-${variant} ${className}`} type="button" {...props}>
      {icon ? <span className="button-icon">{icon}</span> : null}
      <span>{children}</span>
    </button>
  );
}
