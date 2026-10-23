"use client";

import type { SheetInventoryEffect } from "@/services/ingestion.service";

/**
 * F-F.4 — qué le hace al inventario ESTA hoja. **Informa; no pregunta.**
 *
 * Hasta F-F.3 era un selector: además de decir que una hoja era de ventas, había
 * que contestar si esas ventas modificaban el stock. Esa segunda pregunta
 * desapareció porque la primera ya la responde — si la hoja es compra o venta de
 * mercadería, mueve inventario, que es una de las funciones centrales de Véktor.
 *
 * Lo que el usuario SIGUE eligiendo está al lado y no se toca: a qué sección
 * corresponde la hoja, el mapeo de cada columna, y `StockTreatmentChoice` (¿el
 * stock inicial del catálogo genera un gasto y una salida de caja?), que es
 * contable y sigue siendo una decisión suya.
 *
 * **La hoja que no habla de unidades no renderiza nada.** Antes mostraba «Estas
 * cantidades no afectan el inventario» en Gastos_Fijos, Clientes y Proveedores:
 * una respuesta a una pregunta que esas hojas nunca hicieron.
 *
 * El efecto lo sirve el backend (`/inventory-effects`), no una tabla de acá:
 * depende de la entidad de la hoja y de los campos que el mapeo cubre, y cambia
 * mientras el usuario mapea o reasigna la sección. Una copia en el frontend
 * mostraría lo que el importador no hace — el defecto que ya se pagó con el
 * catálogo de campos.
 */
export function InventoryEffectChoice({
  hoja,
  className,
}: {
  hoja: SheetInventoryEffect;
  className?: string;
}) {
  const efecto = hoja.options[0];
  if (!efecto) return null;

  return (
    <div className={className}>
      <p className="text-[11px] text-vk-text-muted">
        Inventario: <span className="text-vk-text-primary">{efecto.label}</span>
      </p>
      {efecto.value === "historical_replay" && (
        <p className="mt-2 rounded border border-vk-warning/20 bg-vk-warning-bg px-3 py-2 text-[11px] text-vk-warning">
          Al confirmar se aplica al stock. Las ventas que no tengan unidades que
          las respalden no se registran: quedan en «Otros» para que las cargues
          cuando completes el inventario.
        </p>
      )}
    </div>
  );
}
