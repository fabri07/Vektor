"use client";

import { useEffect, useRef, useState } from "react";
import { twMerge } from "tailwind-merge";

export interface SelectOption {
  value: string;
  label: string;
}

interface SelectProps {
  options: SelectOption[];
  value?: string;
  onChange: (value: string) => void;
  placeholder?: string;
  disabled?: boolean;
  error?: string;
  label?: string;
  hint?: string;
  id?: string;
}

const ChevronIcon = ({ open }: { open: boolean }) => (
  <svg
    className={twMerge(
      "h-4 w-4 flex-shrink-0 text-vk-text-muted transition-transform duration-150",
      open && "rotate-180",
    )}
    fill="none"
    stroke="currentColor"
    strokeWidth={2}
    viewBox="0 0 24 24"
    aria-hidden="true"
  >
    <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
  </svg>
);

export function Select({
  options,
  value,
  onChange,
  placeholder = "Seleccioná una opción",
  disabled = false,
  error,
  label,
  hint,
  id,
}: SelectProps) {
  const [isOpen, setIsOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const inputId = id ?? label?.toLowerCase().replace(/\s+/g, "-");

  const selectedLabel = options.find((o) => o.value === value)?.label;

  // Cerrar al hacer click fuera
  useEffect(() => {
    if (!isOpen) return;
    function handleClickOutside(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setIsOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [isOpen]);

  // Cerrar con Escape
  useEffect(() => {
    if (!isOpen) return;
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") setIsOpen(false);
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [isOpen]);

  function handleSelect(optValue: string) {
    onChange(optValue);
    setIsOpen(false);
  }

  function handleKeyboardTrigger(e: React.KeyboardEvent) {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      if (!disabled) setIsOpen((prev) => !prev);
    }
  }

  return (
    <div ref={containerRef} className="relative flex flex-col gap-1.5">
      {label && (
        <label
          htmlFor={inputId}
          className="text-xs font-medium text-vk-text-secondary"
        >
          {label}
        </label>
      )}

      {/* Trigger */}
      <div
        id={inputId}
        role="combobox"
        aria-expanded={isOpen}
        aria-haspopup="listbox"
        aria-disabled={disabled}
        tabIndex={disabled ? -1 : 0}
        onClick={() => { if (!disabled) setIsOpen((prev) => !prev); }}
        onKeyDown={handleKeyboardTrigger}
        className={twMerge(
          "relative flex h-9 w-full cursor-pointer select-none items-center justify-between rounded-lg border bg-vk-surface-w px-3 text-sm",
          "transition-colors focus:outline-none focus:ring-2",
          error
            ? "border-vk-danger/60 focus:ring-vk-danger/20"
            : isOpen
              ? "border-vk-blue/40 ring-2 ring-vk-blue/15"
              : "border-vk-border-w focus:border-vk-blue/40 focus:ring-vk-blue/15",
          disabled && "pointer-events-none opacity-40",
        )}
      >
        <span className={selectedLabel ? "text-vk-text-primary" : "text-vk-text-placeholder"}>
          {selectedLabel ?? placeholder}
        </span>
        <ChevronIcon open={isOpen} />
      </div>

      {/* Dropdown */}
      {isOpen && (
        <div
          role="listbox"
          className={[
            "absolute z-[200] mt-1 w-full overflow-hidden rounded-lg border border-vk-border-w bg-vk-surface-w shadow-vk-md",
            // Posicionamiento relativo al contenedor
          ].join(" ")}
          style={{ top: "calc(100% + 4px)", left: 0 }}
        >
          {options.map((opt) => {
            const isSelected = opt.value === value;
            return (
              <div
                key={opt.value}
                role="option"
                aria-selected={isSelected}
                onClick={() => handleSelect(opt.value)}
                className={[
                  "cursor-pointer px-3 py-2 text-sm transition-colors",
                  isSelected
                    ? "bg-vk-blue-subtle font-medium text-vk-blue"
                    : "text-vk-text-primary hover:bg-vk-bg-light",
                ].join(" ")}
              >
                {opt.label}
              </div>
            );
          })}
        </div>
      )}

      {hint && !error && <p className="text-xs text-vk-text-muted">{hint}</p>}
      {error && <p className="text-xs text-vk-danger">{error}</p>}
    </div>
  );
}
