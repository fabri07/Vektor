import type { UnclassifiedRecordResponse } from "@/services/others.service";

function _buildIso(y: string, mo: string, d: string): string {
  const year = Number(y);
  const month = Number(mo);
  const day = Number(d);
  if (!year || month < 1 || month > 12 || day < 1 || day > 31) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${year}-${pad(month)}-${pad(day)}`;
}

/**
 * Normaliza una fecha cruda a `yyyy-mm-dd` para un `<input type="date">`.
 *
 * Sin esto, un valor como "12/03/2024" se seteaba tal cual y el input (que solo
 * acepta ISO) quedaba VACÍO — justo el caso que F6-A genera en volumen al rutear
 * filas a /otros. Convención AR, espejo del parser de backend (date_parsing.py):
 * dd/mm/yyyy ANTES que mm/dd/yyyy (03/04/2026 = 3 de abril). Devuelve "" si no
 * reconoce el formato (el usuario completa a mano).
 */
export function toIsoDate(raw: string): string {
  const s = (raw ?? "").trim();
  if (!s) return "";
  // Ya viene ISO (yyyy-mm-dd, opcionalmente con hora).
  const iso = s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})/);
  if (iso) return _buildIso(iso[1] ?? "", iso[2] ?? "", iso[3] ?? "");
  // dd/mm/yy(yy) o dd-mm-yy(yy). El año de 4 dígitos se prueba PRIMERO: con
  // `\d{2}|\d{4}` la alternación tomaba "20" de "2024" y devolvía el año 2020.
  const m = s.match(/^(\d{1,2})[/-](\d{1,2})[/-](\d{4}|\d{2})(?!\d)/);
  if (!m) return "";
  const first = m[1] ?? "";
  const second = m[2] ?? "";
  const rawYear = m[3] ?? "";
  const year = rawYear.length === 2 ? `20${rawYear}` : rawYear;
  // AR: dd/mm primero; solo si el 2º campo no puede ser mes (>12) y el 1º sí,
  // se interpreta como mm/dd.
  let day = first;
  let month = second;
  if (Number(second) > 12 && Number(first) <= 12) {
    day = second;
    month = first;
  }
  return _buildIso(year, month, day);
}

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
    // Normalizar a ISO: un "12/03/2024" crudo dejaba el <input type="date"> vacío.
    if (!date && /(fecha|date|dia)/.test(k)) date = toIsoDate(value);
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
