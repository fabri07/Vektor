import type { UnclassifiedRecordResponse } from "@/services/others.service";

/** Heurística simple de prellenado desde la fila cruda (solo sugerencia visual). */
export function prefill(record: UnclassifiedRecordResponse): {
  amount: string;
  date: string;
  text: string;
  quantity: string;
  unitCost: string;
} {
  let amount = "";
  let date = "";
  let text = "";
  let quantity = "1";
  let unitCost = "";
  for (const [key, value] of Object.entries(record.row_data)) {
    const k = key.toLowerCase();
    if (!amount && /(monto|importe|total|precio|valor)/.test(k)) amount = value;
    if (!date && /(fecha|date|dia)/.test(k)) date = value;
    if (!text && /(detalle|concepto|descripcion|nombre|producto|item)/.test(k)) text = value;
    if (quantity === "1" && /^(cantidad|cant|unidades|qty)$/.test(k)) quantity = value;
    if (!unitCost && /(costo|precio.?unit|unitario)/.test(k)) unitCost = value;
  }
  return { amount, date, text, quantity, unitCost };
}

export function rowPreview(record: UnclassifiedRecordResponse): string {
  return Object.entries(record.row_data)
    .filter(([, v]) => v !== "")
    .slice(0, 5)
    .map(([k, v]) => `${k}: ${v}`)
    .join(" · ");
}
