import { type ButtonHTMLAttributes, forwardRef } from "react";
import { clsx } from "clsx";
import { twMerge } from "tailwind-merge";

type Variant = "primary" | "secondary" | "ghost" | "danger";
type Size = "sm" | "md" | "lg";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
}

const variantClasses: Record<Variant, string> = {
  primary:
    "bg-gradient-to-r from-vektor-blue-strong to-vektor-teal-deep text-vektor-white hover:shadow-glow focus-visible:ring-vektor-blue/40 active:scale-[0.98]",
  secondary:
    "bg-vektor-surface text-vektor-white hover:border-vektor-blue/40 hover:bg-vektor-surface/90 focus-visible:ring-vektor-blue/30 border border-vektor-border",
  ghost:
    "bg-transparent text-vektor-body hover:text-vektor-white hover:bg-vektor-surface focus-visible:ring-vektor-blue/30",
  danger:
    "bg-vektor-red text-vektor-white hover:bg-vektor-red/90 focus-visible:ring-vektor-red/40",
};

const sizeClasses: Record<Size, string> = {
  sm: "h-8 px-3 text-xs gap-1.5",
  md: "h-9 px-4 text-sm gap-2",
  lg: "h-11 px-6 text-base gap-2",
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  (
    {
      variant = "primary",
      size = "md",
      loading = false,
      disabled,
      className,
      children,
      ...props
    },
    ref,
  ) => {
    return (
      <button
        ref={ref}
        disabled={disabled || loading}
        className={twMerge(
          clsx(
            "inline-flex items-center justify-center rounded-xl font-medium transition-all duration-200",
            "focus-visible:outline-none focus-visible:ring-2",
            "disabled:pointer-events-none disabled:opacity-40",
            variantClasses[variant],
            sizeClasses[size],
            className,
          ),
        )}
        {...props}
      >
        {loading && (
          <svg
            className="h-4 w-4 animate-spin"
            xmlns="http://www.w3.org/2000/svg"
            fill="none"
            viewBox="0 0 24 24"
            aria-hidden="true"
          >
            <circle
              className="opacity-25"
              cx="12"
              cy="12"
              r="10"
              stroke="currentColor"
              strokeWidth="4"
            />
            <path
              className="opacity-75"
              fill="currentColor"
              d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
            />
          </svg>
        )}
        {children}
      </button>
    );
  },
);

Button.displayName = "Button";
