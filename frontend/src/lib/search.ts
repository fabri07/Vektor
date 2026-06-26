/**
 * Utilidades de búsqueda client-side para las tablas.
 * Búsqueda insensible a mayúsculas y a tildes (usuarios AR).
 */

/** Minúsculas + sin tildes: "José" → "jose". */
export function normalizeForSearch(s: string): string {
  return s
    .toLowerCase()
    .normalize("NFD")
    .replace(/\p{Diacritic}/gu, "");
}

/**
 * Convierte cualquier valor de celda a un string buscable.
 * Evita `[object Object]` para objetos/arrays (usa JSON.stringify).
 */
export function safeSearchValue(v: unknown): string {
  if (v == null) return "";
  if (typeof v === "object") {
    try {
      return JSON.stringify(v);
    } catch {
      return "";
    }
  }
  return String(v);
}

/**
 * True si `haystack` contiene `query` (ambos normalizados).
 * Query vacío/whitespace → true (no filtra).
 */
export function matchesQuery(haystack: string, query: string): boolean {
  const q = normalizeForSearch(query.trim());
  if (!q) return true;
  return normalizeForSearch(haystack).includes(q);
}

/**
 * True si alguna de las celdas de una fila matchea `query`.
 * Contrato único de "armar haystack + matchear" reutilizado por SmartTable y
 * las tablas a mano — cambiar el comportamiento (separador, multi-término) acá.
 */
export function matchesRow(values: unknown[], query: string): boolean {
  return matchesQuery(values.map(safeSearchValue).join(" "), query);
}
