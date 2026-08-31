"use client";

import { useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";

import { humanizeColumnLabel } from "@/lib/columnLabels";

const PAGE_SIZE = 5;

/**
 * F-RR Fase 8: filas de ejemplo de una hoja, con paginación anterior/
 * siguiente sobre `preview_rows` (la muestra que ya viaja en
 * `mapping_contexts` — no el archivo completo, así que la paginación es
 * client-side sobre lo que ya se descargó).
 */
export function DataSample({
  rows,
  columns,
  className = "",
}: {
  rows: Record<string, unknown>[];
  /** Orden de columnas a mostrar; si se omite, se derivan de la primera fila. */
  columns?: string[];
  className?: string;
}) {
  const [page, setPage] = useState(0);

  if (rows.length === 0) {
    return (
      <p className={["text-xs text-vk-text-muted", className].join(" ")}>
        No hay filas de ejemplo para esta hoja.
      </p>
    );
  }

  const cols = columns ?? Object.keys(rows[0] ?? {});
  const totalPages = Math.max(Math.ceil(rows.length / PAGE_SIZE), 1);
  const clampedPage = Math.min(page, totalPages - 1);
  const start = clampedPage * PAGE_SIZE;
  const pageRows = rows.slice(start, start + PAGE_SIZE);

  return (
    <div className={className}>
      {/* F10-fix contraste: tarjeta clara SÓLIDA — `bg-vk-surface-w/40` dejaba
          traslucir el canvas oscuro del panel de relectura. */}
      <div className="overflow-x-auto rounded-lg border border-vk-border-w bg-vk-surface-w">
        <table className="w-full min-w-[480px] text-left text-[11px]">
          <thead>
            <tr className="border-b border-vk-border-w bg-vk-bg-light text-vk-text-muted">
              {cols.map((c) => (
                <th key={c} className="whitespace-nowrap px-2 py-1.5 font-medium">
                  {humanizeColumnLabel(c)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-vk-border-w/50">
            {pageRows.map((row, idx) => (
              <tr key={start + idx}>
                {cols.map((c) => (
                  <td key={c} className="whitespace-nowrap px-2 py-1.5 text-vk-text-secondary">
                    {row[c] === null || row[c] === undefined ? (
                      <span className="text-vk-text-muted">—</span>
                    ) : (
                      String(row[c])
                    )}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {totalPages > 1 && (
        <div className="mt-1.5 flex items-center justify-between text-[11px] text-vk-text-muted">
          <span>
            {start + 1}–{Math.min(start + PAGE_SIZE, rows.length)} de {rows.length}
          </span>
          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={() => setPage((p) => Math.max(p - 1, 0))}
              disabled={clampedPage === 0}
              className="flex items-center rounded p-1 hover:bg-vk-bg-light disabled:opacity-40"
              aria-label="Página anterior"
            >
              <ChevronLeft className="h-3.5 w-3.5" />
            </button>
            <button
              type="button"
              onClick={() => setPage((p) => Math.min(p + 1, totalPages - 1))}
              disabled={clampedPage >= totalPages - 1}
              className="flex items-center rounded p-1 hover:bg-vk-bg-light disabled:opacity-40"
              aria-label="Página siguiente"
            >
              <ChevronRight className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
