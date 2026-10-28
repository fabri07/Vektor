import type { UnclassifiedRecordResponse } from "@/services/others.service";

function _buildIso(y: string, mo: string, d: string): string {
  const year = Number(y);
  const month = Number(mo);
  const day = Number(d);
  if (!year || month < 1 || month > 12 || day < 1 || day > 31) return "";
  // Rechazar fechas calendáricamente imposibles (ej. 2024-02-31): construir la
  // fecha y verificar que no haya "rodado" a otro mes.
  const dt = new Date(year, month - 1, day);
  if (dt.getFullYear() !== year || dt.getMonth() !== month - 1 || dt.getDate() !== day) {
    return "";
  }
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${year}-${pad(month)}-${pad(day)}`;
}

/**
 * Normaliza una fecha cruda a `yyyy-mm-dd` para un `<input type="date">`.
 *
 * Sin esto, un valor como "12/03/2024" se seteaba tal cual y el input (que solo
 * acepta ISO) quedaba VACÍO — justo el caso que F6-A genera en volumen al rutear
 * filas a /otros. Sólo se prellena lo INEQUÍVOCO (V-16, criterio del dueño): ISO,
 * o una fecha corta en la que un número no puede ser mes («13/04/2026»). Una
 * fecha corta ambigua («03/04/2026»: 3 de abril o 4 de marzo) nunca se adivina:
 * devuelve "" y la persona la escribe. También "" si no reconoce el formato.
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
  // Año de 2 dígitos: mismo pivote que strptime %y del backend para transacciones
  // (date_parsing.parse_business_datetime sin century_pivot): 00–68 → 20xx,
  // 69–99 → 19xx. Antes el frontend forzaba 20xx y "05/06/99" divergía (2099 vs 1999).
  const year =
    rawYear.length === 2
      ? String((Number(rawYear) <= 68 ? 2000 : 1900) + Number(rawYear))
      : rawYear;
  // Los dos pueden ser mes y dicen fechas distintas: ambigua, no se adivina. Si
  // son iguales («04/04») las dos lecturas dan el mismo día.
  if (Number(first) <= 12 && Number(second) <= 12 && Number(first) !== Number(second)) {
    return "";
  }
  // El que no puede ser mes (>12) es el día.
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

/** B·8b: una referencia de la cola «Vincular a producto» y las ventas que la usan. */
export interface GrupoDeReferencia {
  /** `row_data.producto` con trim, espacios colapsados y minúsculas. */
  clave: string;
  /** Lo que decía el archivo, tal cual (el de la primera venta). */
  referencia: string;
  records: UnclassifiedRecordResponse[];
}

/**
 * Agrupa las ventas pendientes por lo que decía el archivo del producto. Las
 * filas sin `producto` quedan afuera (el backend las rechaza con
 * `SIN_REFERENCIA`). Primero las referencias que más ventas resuelven.
 */
export function agruparPorReferencia(records: UnclassifiedRecordResponse[]): GrupoDeReferencia[] {
  const grupos = new Map<string, GrupoDeReferencia>();
  for (const r of records) {
    const cruda = String(r.row_data?.producto ?? "").trim();
    if (!cruda) continue;
    // Sin acentos ni guiones a propósito (límite documentado): «Coca-Cola» y
    // «coca cola» son dos referencias.
    const clave = cruda.replace(/\s+/g, " ").toLowerCase();
    const grupo = grupos.get(clave);
    if (grupo) grupo.records.push(r);
    else grupos.set(clave, { clave, referencia: cruda, records: [r] });
  }
  return [...grupos.values()].sort((a, b) => b.records.length - a.records.length);
}

/**
 * B·8b: qué queries invalida cerrar la cola. Vincular mueve «Otros», el catálogo
 * (alias, stock) y las ventas; las categorías de producto no.
 */
export function esQueryAfectadaPorVincular(queryKey: readonly unknown[]): boolean {
  return /^(others|products|sales)(-|$)/.test(String(queryKey[0]));
}
