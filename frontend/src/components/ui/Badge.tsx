import { type HTMLAttributes } from "react";
import { twMerge } from "tailwind-merge";

type BadgeVariant = "default" | "success" | "warning" | "danger" | "info";

interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  variant?: BadgeVariant;
}

const variantClasses: Record<BadgeVariant, string> = {
  default: "bg-vektor-surface text-vk-text-secondary border border-vektor-border",
  success: "bg-vk-success-bg text-vk-success border border-vk-success/20",
  warning: "bg-vk-warning-bg text-vk-warning border border-vk-warning/20",
  danger:  "bg-vk-danger-bg text-vk-danger border border-vk-danger/20",
  info:    "bg-vk-info-bg text-vk-info border border-vk-info/20",
};

export function Badge({
  variant = "default",
  className,
  children,
  ...props
}: BadgeProps) {
  return (
    <span
      className={twMerge(
        "inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium transition-all duration-200",
        variantClasses[variant],
        className,
      )}
      {...props}
    >
      {children}
    </span>
  );
}
