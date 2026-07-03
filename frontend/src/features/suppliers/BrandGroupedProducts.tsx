"use client";

import { useState } from "react";
import { Download } from "lucide-react";
import { Badge } from "@/components/ui/Badge";
import { TableSearch } from "@/components/ui/TableSearch";
import { downloadCSV, slugify } from "@/lib/csv";
import { normalizeForSearch } from "@/lib/search";
import { formatDateTime } from "@/lib/datetime";
import { formatARS } from "@/features/dashboard/dashboardData";
import type {
  SupplierBrandGroup,
  SupplierProductPurchase,
} from "@/services/suppliers.service";

// Etiqueta del grupo sin marca DENTRO de la ficha del proveedor. El dashboard
// (breakdown) llama a este mismo concepto "Sin marca" (materializado en backend):
// son contextos distintos, no unificar sin decisión de producto.
const GENERIC_LABEL = "Productos genéricos";
const GENERIC_KEY = "__generic";

/** Cuántas filas se muestran por grupo antes de pedir "Mostrar todos". */
const GROUP_ROW_CAP = 25;

/** Clave estable del grupo (por construcción hay a lo sumo un grupo sin marca). */
function groupKey(group: SupplierBrandGroup): string {
  return group.brand ?? GENERIC_KEY;
}

/** Subtotal ($) del grupo COMPLETO — no se ve afectado por búsqueda ni cap. */
export function groupSubtotal(group: SupplierBrandGroup): number {
  return group.products.reduce(
    (sum, p) => sum + p.total_qty * p.unit_price,
    0,
  );
}

export function BrandGroupedProducts({
  groups,
  supplierName,
}: {
  groups: SupplierBrandGroup[];
  supplierName: string;
}) {
  const [search, setSearch] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  // El CSV exporta SIEMPRE todo: sin filtro de búsqueda ni cap por grupo.
  function exportCSV() {
    const headers = [
      "Marca",
      "Oficial",
      "Producto",
      "Última compra",
      "Cantidad",
      "Precio unit.",
    ];
    const rows = groups.flatMap((group) =>
      group.products.map((p) => [
        group.brand ?? GENERIC_LABEL,
        group.is_official ? "Sí" : "No",
        p.name,
        formatDateTime(p.last_purchase_at),
        String(p.total_qty),
        String(p.unit_price),
      ]),
    );
    downloadCSV(`proveedor-${slugify(supplierName)}-productos`, headers, rows);
  }

  if (groups.length === 0) return null;

  const term = normalizeForSearch(search.trim());
  const searching = term.length > 0;

  // Con búsqueda: filtra por nombre de producto en TODOS los grupos y oculta los
  // que quedan sin matches. Sin búsqueda: los grupos se muestran todos.
  const visibleGroups = groups
    .map((group) => ({
      group,
      matched: searching
        ? group.products.filter((p) => normalizeForSearch(p.name).includes(term))
        : group.products,
    }))
    .filter(({ matched }) => !searching || matched.length > 0);

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <TableSearch
          value={search}
          onChange={setSearch}
          placeholder="Buscar producto…"
          className="w-full sm:w-64"
        />
        <button
          onClick={exportCSV}
          className="flex items-center gap-1.5 self-end rounded-lg border border-vk-border-w bg-vk-surface-w px-3 py-1.5 text-xs text-vk-text-secondary transition-colors hover:text-vk-text-primary sm:self-auto"
        >
          <Download className="h-3.5 w-3.5" />
          Exportar CSV
        </button>
      </div>

      {visibleGroups.length === 0 ? (
        <p className="rounded-lg border border-vk-border-w bg-vk-surface-w px-4 py-6 text-center text-sm text-vk-text-secondary">
          Sin resultados para “{search.trim()}”
        </p>
      ) : (
        visibleGroups.map(({ group, matched }) => {
          const key = groupKey(group);
          const isExpanded = expanded.has(key);
          // Sin búsqueda: cap de 25 filas + "Mostrar todos". Con búsqueda: todos los matches.
          const rows =
            searching || isExpanded ? matched : matched.slice(0, GROUP_ROW_CAP);
          const hiddenCount = matched.length - rows.length;
          const label = group.brand ?? GENERIC_LABEL;

          return (
            <section
              key={key}
              className="overflow-hidden rounded-lg border border-vk-border-w bg-vk-surface-w"
            >
              <header className="flex flex-wrap items-center justify-between gap-2 border-b border-vk-border-w px-4 py-3">
                <div className="flex items-center gap-2">
                  <h3 className="font-display text-sm font-semibold text-vk-text-primary">
                    {label}
                  </h3>
                  {group.is_official && <Badge variant="info">Oficial</Badge>}
                </div>
                <span className="text-xs text-vk-text-secondary">
                  {group.products.length}{" "}
                  {group.products.length === 1 ? "producto" : "productos"} ·{" "}
                  {formatARS(groupSubtotal(group))}
                </span>
              </header>

              <div className="overflow-x-auto">
                <table className="w-full min-w-full border-collapse text-sm">
                  <thead>
                    <tr className="border-b border-vk-border-w">
                      {["Producto", "Última compra", "Cantidad", "Precio unit."].map(
                        (h) => (
                          <th
                            key={h}
                            scope="col"
                            className="whitespace-nowrap px-4 py-2.5 text-left text-xs font-semibold uppercase tracking-wider text-vk-text-secondary"
                          >
                            {h}
                          </th>
                        ),
                      )}
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((p: SupplierProductPurchase) => (
                      <tr
                        key={p.product_id}
                        className="border-b border-vk-border-w/60 transition-colors last:border-b-0 hover:bg-vk-bg-light"
                      >
                        <td className="whitespace-nowrap px-4 py-2.5">
                          <span className="font-medium text-vk-text-primary">
                            {p.name}
                          </span>
                        </td>
                        <td className="whitespace-nowrap px-4 py-2.5">
                          <span className="text-vk-text-secondary">
                            {formatDateTime(p.last_purchase_at)}
                          </span>
                        </td>
                        <td className="whitespace-nowrap px-4 py-2.5">
                          <span className="text-vk-text-primary">
                            {p.total_qty}
                          </span>
                        </td>
                        <td className="whitespace-nowrap px-4 py-2.5">
                          <span className="text-vk-text-primary">
                            {formatARS(p.unit_price)}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {hiddenCount > 0 && (
                <div className="border-t border-vk-border-w px-4 py-2.5">
                  <button
                    onClick={() =>
                      setExpanded((prev) => new Set(prev).add(key))
                    }
                    className="text-xs font-medium text-vk-blue transition-colors hover:underline"
                  >
                    Mostrar todos ({group.products.length})
                  </button>
                </div>
              )}
            </section>
          );
        })
      )}
    </div>
  );
}
