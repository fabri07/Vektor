"use client";

import type { StockTreatment } from "@/services/ingestion.service";

/**
 * ¿El archivo trae stock/productos? Se decide por el contenido del
 * `parsed_summary_json`: tipo inferido "stock", flag `has_producto`, bucket de
 * productos (`stock_detectado`/`productos_detectados`) o algún contexto de mapeo
 * con `entity_type === "product"` (multi-hoja / texto / imagen).
 */
export function summaryHasStock(
  summary: Record<string, unknown> | null | undefined,
): boolean {
  if (!summary) return false;

  const inferred =
    typeof summary.inferred_type === "string" ? summary.inferred_type : "";
  if (inferred === "stock") return true;
  if (summary.has_producto === true) return true;

  const stockDet = summary.stock_detectado;
  if (Array.isArray(stockDet) && stockDet.length > 0) return true;

  const prodDet = summary.productos_detectados;
  if (Array.isArray(prodDet) && prodDet.length > 0) return true;

  const contexts = Array.isArray(summary.mapping_contexts)
    ? summary.mapping_contexts
    : [];
  if (
    contexts.some(
      (c) => (c as { entity_type?: string } | null)?.entity_type === "product",
    )
  ) {
    return true;
  }

  return false;
}

const OPTIONS: Array<{ value: StockTreatment; title: string; desc: string }> = [
  {
    value: "opening_balance",
    title: "Ya lo tenía (saldo de apertura)",
    desc: "Solo carga el inventario. No genera gasto ni salida de caja.",
  },
  {
    value: "purchase",
    title: "Lo compré",
    desc: "Registra el gasto de mercadería (COGS) y la baja de caja.",
  },
];

/**
 * Selector "¿Este stock ya lo tenías o lo compraste?" para el confirm de un
 * archivo que trae stock/productos. Default recomendado: "opening_balance".
 */
export function StockTreatmentChoice({
  value,
  onChange,
  className,
}: {
  value: StockTreatment;
  onChange: (v: StockTreatment) => void;
  className?: string;
}) {
  return (
    <div className={className}>
      <p className="mb-2 text-xs font-semibold text-vk-text-primary">
        ¿Este stock ya lo tenías o lo compraste?
      </p>
      <div className="flex flex-col gap-2 sm:flex-row">
        {OPTIONS.map((opt) => {
          const active = value === opt.value;
          return (
            <button
              key={opt.value}
              type="button"
              onClick={() => onChange(opt.value)}
              aria-pressed={active}
              className={`flex-1 rounded-lg border px-3 py-2 text-left transition-colors ${
                active
                  ? "border-vk-blue bg-vk-info-bg"
                  : "border-vk-border-w hover:bg-vk-bg-light"
              }`}
            >
              <span
                className={`block text-xs font-medium ${
                  active ? "text-vk-blue" : "text-vk-text-primary"
                }`}
              >
                {opt.title}
              </span>
              <span className="mt-0.5 block text-[11px] text-vk-text-muted">
                {opt.desc}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
