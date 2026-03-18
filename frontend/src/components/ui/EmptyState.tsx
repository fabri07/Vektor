import type { ReactNode } from "react";
import Link from "next/link";

interface EmptyStateAction {
  label: string;
  href?: string;
  onClick?: () => void;
}

interface EmptyStateProps {
  icon?: ReactNode;
  title: string;
  description?: string;
  action?: EmptyStateAction;
  variant?: "default" | "compact";
}

const DefaultIcon = () => (
  <svg
    className="h-6 w-6 text-vk-text-muted"
    fill="none"
    stroke="currentColor"
    strokeWidth={1.5}
    viewBox="0 0 24 24"
  >
    <path
      strokeLinecap="round"
      strokeLinejoin="round"
      d="M2.25 13.5h3.86a2.25 2.25 0 012.012 1.244l.256.512a2.25 2.25 0 002.013 1.244h3.218a2.25 2.25 0 002.013-1.244l.256-.512a2.25 2.25 0 012.013-1.244h3.859m-19.5.338V18a2.25 2.25 0 002.25 2.25h15A2.25 2.25 0 0021.75 18v-4.162c0-.224-.034-.447-.1-.661L19.24 5.338a2.25 2.25 0 00-2.15-1.588H6.911a2.25 2.25 0 00-2.15 1.588L2.35 13.177a2.25 2.25 0 00-.1.661z"
    />
  </svg>
);

export function EmptyState({
  icon,
  title,
  description,
  action,
  variant = "default",
}: EmptyStateProps) {
  const isCompact = variant === "compact";

  const iconSize = isCompact ? "h-10 w-10" : "h-14 w-14";
  const padding = isCompact ? "p-6" : "p-10";
  const titleClass = isCompact
    ? "text-sm font-semibold text-vk-text-primary"
    : "text-base font-semibold text-vk-text-primary";
  const descClass = isCompact
    ? "mt-1 text-xs text-vk-text-muted"
    : "mt-2 text-sm text-vk-text-muted";

  return (
    <div className={`flex flex-col items-center text-center ${padding}`}>
      <div
        className={`mb-4 flex ${iconSize} flex-shrink-0 items-center justify-center rounded-full bg-vk-bg-light`}
      >
        {icon ?? <DefaultIcon />}
      </div>

      <p className={titleClass}>{title}</p>

      {description && <p className={descClass}>{description}</p>}

      {action && (
        <div className="mt-4">
          {action.href ? (
            <Link
              href={action.href}
              className="inline-flex items-center justify-center rounded-lg bg-vk-blue px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-vk-blue-hover focus:outline-none focus:ring-2 focus:ring-vk-blue/40"
            >
              {action.label}
            </Link>
          ) : (
            <button
              type="button"
              onClick={action.onClick}
              className="inline-flex items-center justify-center rounded-lg bg-vk-blue px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-vk-blue-hover focus:outline-none focus:ring-2 focus:ring-vk-blue/40"
            >
              {action.label}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
