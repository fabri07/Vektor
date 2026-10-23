import type { SaleEntryResponse } from "@/services/sales.service";

/**
 * M·3-2: el margen de una venta tal como lo manda el backend (sólo a dueño o
 * admin). Sólo formatea: nunca calcula ni muestra un 0 inventado.
 */
export function textoMargenDeVenta(
  v: unknown,
  row: Pick<SaleEntryResponse, "margen_estado" | "margen_motivo">,
): string {
  if (v == null) {
    return row.margen_estado === "ausente" ? `— (${row.margen_motivo ?? "sin costo"})` : "—";
  }
  const pct = `${Number(v).toFixed(1).replace(".", ",")}%`;
  return row.margen_estado === "estimado" ? `${pct} estimado` : pct;
}
