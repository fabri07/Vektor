/**
 * Utilidades de exportación CSV compartidas por las tablas.
 * Fuente única de "escapar valor + armar blob + descargar" — antes duplicada en
 * SmartTable y BrandGroupedProducts. Excel es-AR necesita el BOM UTF-8 para
 * leer bien los acentos.
 */

/** Escapa un valor para CSV: entrecomilla si tiene coma, comilla o salto de línea. */
export function toCSVValue(val: unknown): string {
  const s = val == null ? "" : String(val);
  if (s.includes(",") || s.includes('"') || s.includes("\n")) {
    return `"${s.replace(/"/g, '""')}"`;
  }
  return s;
}

/** Minúsculas, sin tildes ni caracteres raros: "Distribuidora Sur" → "distribuidora-sur". */
export function slugify(value: string): string {
  return (
    value
      .toLowerCase()
      .normalize("NFD")
      .replace(/\p{Diacritic}/gu, "")
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "") || "proveedor"
  );
}

/**
 * Descarga un CSV con BOM UTF-8. `filename` va SIN fecha ni extensión: se le
 * agrega `-YYYY-MM-DD.csv` (si ya trae `.csv`, se lo saca para no duplicarlo),
 * así dos exports del mismo origen en días distintos no se pisan.
 */
export function downloadCSV(
  filename: string,
  headers: string[],
  rows: string[][],
): void {
  const csv = [headers, ...rows]
    .map((r) => r.map(toCSVValue).join(","))
    .join("\n");
  const blob = new Blob(["﻿" + csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const base = filename.replace(/\.csv$/i, "");
  const a = document.createElement("a");
  a.href = url;
  a.download = `${base}-${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}
