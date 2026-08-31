/**
 * El parser nombra una celda de encabezado vacía `col_{i}` (`file_parsing.py`,
 * `customer_extraction_service.py`, `remito_extraction_service.py`,
 * `supplier_extraction_service.py`) — esa es la CLAVE interna estable que
 * viaja en mapeos y targets. Nunca se cambia. Esto solo transforma el texto
 * que ve el usuario.
 */
const RAW_COLUMN_RE = /^col_(\d+)$/;

export function humanizeColumnLabel(column: string): string {
  const match = RAW_COLUMN_RE.exec(column);
  if (!match) return column;
  return `Columna sin encabezado ${match[1]}`;
}
