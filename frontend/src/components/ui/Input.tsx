import { type InputHTMLAttributes, forwardRef } from "react";
import { twMerge } from "tailwind-merge";

interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  label?: string;
  error?: string;
  hint?: string;
}

export const Input = forwardRef<HTMLInputElement, InputProps>(
  ({ label, error, hint, className, id, ...props }, ref) => {
    const inputId = id ?? label?.toLowerCase().replace(/\s+/g, "-");

    return (
      <div className="flex flex-col gap-1.5">
        {label && (
          <label
            htmlFor={inputId}
            className="text-xs font-medium text-vektor-body"
          >
            {label}
          </label>
        )}
        <input
          ref={ref}
          id={inputId}
          className={twMerge(
            "h-10 w-full rounded-xl border bg-vektor-surface px-3 text-sm text-vektor-white",
            "placeholder:text-vk-text-placeholder",
            "transition-all duration-200 focus:outline-none focus:ring-2",
            error
              ? "border-vk-danger/60 focus:ring-vk-danger/20"
              : "border-vk-border-w focus:border-vk-blue/40 focus:ring-vk-blue/15",
            "disabled:pointer-events-none disabled:opacity-40",
            className,
          )}
          {...props}
        />
        {hint && !error && (
          <p className="text-xs text-vk-text-muted">{hint}</p>
        )}
        {error && (
          <p className="text-xs text-vk-danger">{error}</p>
        )}
      </div>
    );
  },
);

Input.displayName = "Input";
