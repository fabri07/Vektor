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
    title: "Ya la tenía en el negocio",
    desc: "Carga el inventario y nada más. No descuenta plata ni suma un gasto.",
  },
  {
    value: "purchase",
    title: "La compré ahora",
    desc: "Además del inventario, registra el gasto de mercadería y la salida de caja.",
  },
];

/**
 * Origen del stock de UNA hoja de productos.
 *
 * Se pregunta por hoja, no por archivo: un mismo Excel puede traer el catálogo
 * de lo que el negocio ya tenía y, aparte, las compras del mes. Con un único
 * valor global había que mentir en una de las dos, y elegir "compra" generaba un
 * gasto por cada producto del catálogo aunque esos costos ya estuvieran cargados
 * como egresos en el libro diario.
 *
 * Default: "ya la tenía" — es el que NO toca caja, así que equivocarse ahí no
 * inventa un gasto.
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
      <p className="text-xs font-semibold text-vk-text-primary">
        Esta mercadería, ¿ya la tenías o la compraste?
      </p>
      <p className="mb-2 text-[11px] text-vk-text-muted">
        Define si además del stock hay que registrar el gasto y la salida de caja.
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
