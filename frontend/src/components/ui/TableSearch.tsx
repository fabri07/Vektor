"use client";

import { Search, X } from "lucide-react";

interface TableSearchProps {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  className?: string;
}

/**
 * Buscador de tabla: input claro (fondo blanco) consistente con la toolbar de
 * SmartTable. Ícono de lupa + botón "×" para limpiar cuando hay texto.
 */
export function TableSearch({
  value,
  onChange,
  placeholder = "Buscar…",
  className,
}: TableSearchProps) {
  return (
    <div className={`relative ${className ?? ""}`}>
      <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-vk-text-muted" />
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        aria-label={placeholder}
        className="w-full rounded-lg border border-vk-border-w bg-vk-surface-w py-1.5 pl-8 pr-8 text-sm text-vk-text-primary placeholder:text-vk-text-muted focus:outline-none focus:ring-2 focus:ring-vk-blue/20"
      />
      {value && (
        <button
          type="button"
          onClick={() => onChange("")}
          aria-label="Limpiar búsqueda"
          className="absolute right-2 top-1/2 -translate-y-1/2 text-vk-text-muted transition-colors hover:text-vk-text-primary"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      )}
    </div>
  );
}
