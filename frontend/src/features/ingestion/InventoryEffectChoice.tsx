"use client";

import type { SheetInventoryEffect } from "@/services/ingestion.service";

/**
 * F-H3.e — qué le hace al inventario ESTA hoja.
 *
 * Eje separado de `StockTreatmentChoice`, que está al lado y responde otra cosa:
 * aquel es contable (¿el stock inicial del catálogo genera un gasto y una salida
 * de caja?), éste es de unidades. Fusionarlos haría que elegir "las ventas
 * descuentan" declare en silencio que el catálogo genera COGS.
 *
 * **Las opciones y el default los sirve el backend** (`/inventory-effects`), no
 * una lista de acá: dependen de la entidad de la hoja y de los campos que el
 * mapeo cubre, y cambian mientras el usuario mapea. Una copia en el frontend
 * ofrecería modos que el importador no honra — el defecto que ya se pagó con el
 * catálogo de campos.
 *
 * El alcance es la HOJA: un catálogo puede dejar el stock en su saldo mientras
 * las ventas de la hoja de al lado no lo descuentan. Por eso el copy dice "estas
 * filas" y nunca "este archivo".
 */
export function InventoryEffectChoice({
  hoja,
  value,
  onChange,
  className,
}: {
  hoja: SheetInventoryEffect;
  value: string;
  onChange: (v: string) => void;
  className?: string;
}) {
  const elegido = hoja.options.find((o) => o.value === value) ?? hoja.options[0];

  // Una sola opción = no hay decisión que tomar (la hoja no habla de unidades).
  // Se informa qué va a pasar y no se ofrece un control que no hace nada.
  if (hoja.options.length <= 1) {
    return (
      <p className={`text-[11px] text-vk-text-muted ${className ?? ""}`}>
        Inventario: {elegido?.label ?? "sin efecto"}
      </p>
    );
  }

  return (
    <div className={className}>
      <p className="text-xs font-semibold text-vk-text-primary">
        ¿Qué hacemos con el inventario de esta hoja?
      </p>
      <p className="mb-2 text-[11px] text-vk-text-muted">
        Podés dejar que Véktor sólo calcule el impacto, o aplicarlo al stock.
      </p>
      <div className="flex flex-col gap-2">
        {hoja.options.map((opt) => {
          const active = value === opt.value;
          return (
            <button
              key={opt.value}
              type="button"
              onClick={() => onChange(opt.value)}
              aria-pressed={active}
              className={`rounded-lg border px-3 py-2 text-left transition-colors ${
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
                {opt.label}
              </span>
            </button>
          );
        })}
      </div>
      {value === "historical_replay" && (
        <p className="mt-2 rounded border border-vk-warning/20 bg-vk-warning-bg px-3 py-2 text-[11px] text-vk-warning">
          Es el único modo que modifica el stock al confirmar. Las ventas que no
          tengan unidades que las respalden no se registran: quedan en «Otros»
          para que las cargues cuando completes el inventario.
        </p>
      )}
    </div>
  );
}
