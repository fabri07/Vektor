"use client";

import type { InventoryImpactItem } from "@/services/ingestion.service";

/**
 * F-H3.c — el impacto que el archivo TENDRÍA sobre el inventario.
 *
 * El punto del panel es que el usuario pueda decidir si aplicar la historia del
 * archivo, así que muestra la cuenta completa (saldo inicial → movimientos →
 * saldo final) y no sólo el resultado: un saldo final solo no deja ver si el
 * número viene de las compras, de las ventas o de un catálogo que declaró otro
 * saldo.
 *
 * Nada de esto se aplicó. El copy lo dice en presente condicional a propósito
 * ("quedaría", "pasaría") — decirlo en pasado sugeriría que el stock ya cambió.
 */

function formatearFecha(iso: string | null | undefined): string {
  if (!iso) return "";
  // Las fechas vienen como `YYYY-MM-DD` (día de negocio, sin hora). Se parte el
  // string en vez de usar `new Date`, que sobre un ISO sin zona interpreta UTC y
  // en Argentina puede mostrar el día anterior.
  const [anio, mes, dia] = iso.split("-");
  return dia && mes && anio ? `${dia}/${mes}/${anio}` : iso;
}

export function InventoryImpactPanel({
  items,
  total,
}: {
  items: InventoryImpactItem[];
  /** Total de productos con impacto, incluidos los que no se listan. */
  total: number;
}) {
  if (items.length === 0) return null;

  const conNegativo = items.filter((p) => p.primer_negativo_en);
  const ocultos = Math.max(0, total - items.length);

  return (
    <section className="mt-3 rounded-lg border border-vektor-border bg-vektor-surface p-4">
      <h3 className="text-sm font-medium text-vektor-ink">
        Impacto en el inventario si aplicaras esta historia
      </h3>
      <p className="mt-1 text-xs text-vektor-ink/60">
        El stock <strong>no se modificó</strong>. Esto es lo que pasaría si
        aplicaras las compras y ventas del archivo, día por día.
      </p>

      {conNegativo.length > 0 && (
        <p className="mt-2 rounded border border-vk-warning/20 bg-vk-warning-bg px-3 py-2 text-xs text-vk-warning">
          {conNegativo.length === 1
            ? "1 producto quedaría"
            : `${conNegativo.length} productos quedarían`}{" "}
          con stock negativo en algún momento. Suele significar que falta cargar
          compras anteriores, no que el stock de hoy esté mal.
        </p>
      )}

      <div className="mt-3 overflow-x-auto">
        <table className="w-full min-w-[34rem] text-left text-xs">
          <thead className="text-vektor-ink/60">
            <tr>
              <th className="pb-2 pr-3 font-medium">Producto</th>
              <th className="pb-2 pr-3 text-right font-medium">Stock inicial</th>
              <th className="pb-2 pr-3 text-right font-medium">Compras</th>
              <th className="pb-2 pr-3 text-right font-medium">Ventas</th>
              <th className="pb-2 pr-3 text-right font-medium">Stock final</th>
              <th className="pb-2 font-medium">Mínimo</th>
            </tr>
          </thead>
          <tbody>
            {items.map((p) => {
              const tocaNegativo = Boolean(p.primer_negativo_en);
              return (
                <tr
                  key={p.product_id}
                  className="border-t border-vektor-border/50 text-vektor-ink"
                >
                  <td className="py-2 pr-3">{p.product_name}</td>
                  <td className="py-2 pr-3 text-right tabular-nums">
                    {p.saldo_inicial}
                  </td>
                  <td className="py-2 pr-3 text-right tabular-nums">
                    {p.compradas > 0 ? `+${p.compradas}` : "—"}
                  </td>
                  <td className="py-2 pr-3 text-right tabular-nums">
                    {p.vendidas > 0 ? `−${p.vendidas}` : "—"}
                  </td>
                  <td
                    className={`py-2 pr-3 text-right tabular-nums ${
                      p.saldo_final < 0 ? "text-vk-danger" : ""
                    }`}
                  >
                    {p.saldo_final}
                  </td>
                  <td className="py-2 text-vektor-ink/70">
                    {tocaNegativo ? (
                      <span className="text-vk-warning">
                        {p.minimo} el {formatearFecha(p.primer_negativo_en)}
                      </span>
                    ) : (
                      p.minimo
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {ocultos > 0 && (
        <p className="mt-2 text-xs text-vektor-ink/60">
          Mostrando {items.length} de {total} productos con impacto (los que
          quedan en negativo aparecen primero).
        </p>
      )}
    </section>
  );
}
