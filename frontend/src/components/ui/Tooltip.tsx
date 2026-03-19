// TODO: agregar fallback onClick para dispositivos touch
// (Véktor es desktop-first en v1 — tooltip purely informativo)

import type { ReactNode } from "react";

type TooltipPosition = "top" | "bottom" | "left" | "right";

interface TooltipProps {
  content: string;
  position?: TooltipPosition;
  children: ReactNode;
}

// ── Estilos por posición ──────────────────────────────────────────────────────
// Cada posición define:
//   - offset del tooltip respecto al trigger
//   - transform para centrarlo
//   - clases de la flecha (triángulo CSS con border trick)

const POSITION_STYLES: Record<
  TooltipPosition,
  { tooltip: string; arrow: string }
> = {
  top: {
    tooltip: "bottom-full left-1/2 mb-2 -translate-x-1/2",
    arrow: [
      "absolute left-1/2 top-full -translate-x-1/2",
      "border-4 border-transparent border-t-vk-navy",
    ].join(" "),
  },
  bottom: {
    tooltip: "top-full left-1/2 mt-2 -translate-x-1/2",
    arrow: [
      "absolute bottom-full left-1/2 -translate-x-1/2",
      "border-4 border-transparent border-b-vk-navy",
    ].join(" "),
  },
  left: {
    tooltip: "right-full top-1/2 mr-2 -translate-y-1/2",
    arrow: [
      "absolute left-full top-1/2 -translate-y-1/2",
      "border-4 border-transparent border-l-vk-navy",
    ].join(" "),
  },
  right: {
    tooltip: "left-full top-1/2 ml-2 -translate-y-1/2",
    arrow: [
      "absolute right-full top-1/2 -translate-y-1/2",
      "border-4 border-transparent border-r-vk-navy",
    ].join(" "),
  },
};

export function Tooltip({ content, position = "top", children }: TooltipProps) {
  const { tooltip: tooltipPos, arrow: arrowPos } = POSITION_STYLES[position];

  return (
    <span className="group relative inline-flex">
      {children}

      {/* Tooltip — aparece en hover con delay 300ms, CSS puro */}
      <span
        role="tooltip"
        className={[
          "pointer-events-none absolute z-50 whitespace-nowrap",
          "rounded px-2.5 py-1.5",
          "bg-vk-navy text-xs font-medium text-white",
          // Entrada con delay — opacity + scale desde 95%
          "opacity-0 scale-95 transition-all duration-150",
          "group-hover:opacity-100 group-hover:scale-100",
          "[transition-delay:300ms] group-hover:[transition-delay:300ms]",
          tooltipPos,
        ].join(" ")}
      >
        {content}
        {/* Flecha */}
        <span aria-hidden="true" className={arrowPos} />
      </span>
    </span>
  );
}
