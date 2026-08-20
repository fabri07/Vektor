"use client";

import type { RereadImpactProjection } from "@/services/ingestion.service";

/**
 * F-RR Fase 8: las 5 categorías del impacto proyectado en el vínculo
 * venta/compra↔producto (ver `reread_service.estimate_unlinked_products`),
 * ANTES de aplicar — nace del incidente ASTERIA, donde el resumen de reread
 * decía "sin_producto: 0" mientras la base tenía miles de ventas/gastos sin
 * producto real.
 */

interface Row {
  label: string;
  value: number;
  tone: "success" | "warning" | "danger" | "muted";
}

const TONE_CLASS: Record<Row["tone"], string> = {
  success: "text-vk-success",
  warning: "text-vk-warning",
  danger: "text-vk-danger",
  muted: "text-vk-text-secondary",
};

export function ImportImpactSummary({
  impact,
  className = "",
}: {
  impact: RereadImpactProjection;
  className?: string;
}) {
  const allRows: Row[] = [
    { label: "Ventas vinculadas a producto", value: impact.ventas_con_producto, tone: "success" },
    { label: "Ventas sin producto", value: impact.ventas_sin_producto, tone: "danger" },
    { label: "Compras vinculadas a producto", value: impact.compras_vinculadas, tone: "success" },
    { label: "Compras que crean producto nuevo", value: impact.compras_producto_nuevo, tone: "warning" },
    { label: "Compras sin producto (van a Otros)", value: impact.compras_sin_producto, tone: "danger" },
    {
      label: "Compras bloqueadas (falta columna de cantidad)",
      value: impact.compras_gate_bloqueado,
      tone: "danger",
    },
    {
      label: "Movimientos que no necesitan producto",
      value: impact.movimientos_sin_producto_esperado,
      tone: "muted",
    },
  ];
  const rows = allRows.filter((r) => r.value > 0);

  if (rows.length === 0) {
    return (
      <p className={["text-xs text-vk-text-muted", className].join(" ")}>
        Sin impacto proyectado en el vínculo con productos.
      </p>
    );
  }

  return (
    <ul className={["space-y-1", className].join(" ")}>
      {rows.map((r) => (
        <li
          key={r.label}
          className="flex items-center justify-between rounded border border-vk-border-w/60 bg-vk-surface-w/40 px-2 py-1 text-xs"
        >
          <span className="text-vk-text-secondary">{r.label}</span>
          <span className={`font-semibold ${TONE_CLASS[r.tone]}`}>{r.value}</span>
        </li>
      ))}
    </ul>
  );
}
