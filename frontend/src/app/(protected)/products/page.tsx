"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { PageWrapper } from "@/components/layout/PageWrapper";
import { StatCard } from "@/components/ui/StatCard";
import { Table } from "@/components/ui/Table";
import { Badge } from "@/components/ui/Badge";
import { EmptyState } from "@/components/ui/EmptyState";
import { productsService, type ProductResponse } from "@/services/products.service";

type StockFilter = "all" | "ok" | "low" | "out";

function formatARS(value: number): string {
  return new Intl.NumberFormat("es-AR", {
    style: "currency",
    currency: "ARS",
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  }).format(value);
}

function getStockBadge(product: ProductResponse) {
  if (product.stock_units === 0) {
    return <Badge variant="danger">Sin stock</Badge>;
  }
  if (product.is_low_stock) {
    return <Badge variant="warning">Stock bajo</Badge>;
  }
  return <Badge variant="success">OK</Badge>;
}

function stockSort(a: ProductResponse, b: ProductResponse): number {
  // out → low → ok
  const rank = (p: ProductResponse) =>
    p.stock_units === 0 ? 0 : p.is_low_stock ? 1 : 2;
  return rank(a) - rank(b);
}

const STOCK_FILTER_OPTIONS: { value: StockFilter; label: string }[] = [
  { value: "all", label: "Todos" },
  { value: "ok", label: "OK" },
  { value: "low", label: "Stock bajo" },
  { value: "out", label: "Sin stock" },
];

const COLUMNS = [
  {
    key: "name",
    header: "Producto",
    render: (v: unknown) => (
      <span className="font-medium text-vk-text-primary">{String(v)}</span>
    ),
  },
  {
    key: "sku",
    header: "SKU",
    render: (v: unknown) => String(v ?? "").trim() || "—",
  },
  {
    key: "category",
    header: "Categoría",
    render: (v: unknown) => String(v ?? "").trim() || "—",
  },
  {
    key: "stock_units",
    header: "Stock",
    render: (v: unknown) => String(v),
  },
  {
    key: "sale_price_ars",
    header: "Precio",
    render: (v: unknown) => formatARS(Number(v)),
  },
  {
    key: "margin_pct",
    header: "Margen",
    render: (v: unknown) =>
      v != null ? `${Number(v).toFixed(1)}%` : "—",
  },
  {
    key: "_status",
    header: "Estado",
    render: (_: unknown, row: Record<string, unknown>) =>
      getStockBadge(row as unknown as ProductResponse),
  },
];

export default function ProductsPage() {
  const [stockFilter, setStockFilter] = useState<StockFilter>("all");

  const { data: products = [], isLoading, isError } = useQuery({
    queryKey: ["products-list"],
    queryFn: () => productsService.getAllProducts({ is_active: true }),
    staleTime: 2 * 60 * 1000,
  });

  // KPIs
  const totalActive = products.length;
  const lowStockCount = products.filter((p) => p.is_low_stock && p.stock_units > 0).length;
  const outOfStockCount = products.filter((p) => p.stock_units === 0).length;
  const inventoryValue = products.reduce(
    (s, p) => s + p.stock_units * (p.unit_cost_ars ?? 0),
    0,
  );

  // Apply filter
  const filtered = products.filter((p) => {
    if (stockFilter === "ok") return p.stock_units > 0 && !p.is_low_stock;
    if (stockFilter === "low") return p.is_low_stock && p.stock_units > 0;
    if (stockFilter === "out") return p.stock_units === 0;
    return true;
  });

  // Add _status key for the table (unused by render, just for key lookup)
  const tableData = [...filtered]
    .sort(stockSort)
    .map((p) => ({ ...p, _status: null }));

  return (
    <PageWrapper title="Productos">
      {/* Filter */}
      <div className="flex items-center gap-3">
        <label className="text-sm text-vk-text-muted">Estado:</label>
        <select
          value={stockFilter}
          onChange={(e) => setStockFilter(e.target.value as StockFilter)}
          className="rounded-lg border border-vk-border-w bg-vk-surface-w px-3 py-1.5 text-sm text-vk-text-primary focus:outline-none focus:ring-2 focus:ring-vk-blue/20"
        >
          {STOCK_FILTER_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </div>

      {/* KPIs */}
      {isLoading ? (
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          {[...Array<number>(4)].map((_, i) => (
            <div
              key={i}
              className="h-24 animate-pulse rounded-lg border border-vk-border-w bg-vk-surface-w"
            />
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          <StatCard label="Productos activos" value={totalActive} />
          <StatCard
            label="Stock bajo"
            value={lowStockCount}
            trend={lowStockCount > 0 ? "down" : "neutral"}
            trendValue={lowStockCount > 0 ? "reponer pronto" : undefined}
          />
          <StatCard
            label="Sin stock"
            value={outOfStockCount}
            trend={outOfStockCount > 0 ? "down" : "neutral"}
          />
          <StatCard
            label="Valor inventario"
            value={inventoryValue > 0 ? formatARS(inventoryValue) : "—"}
            sublabel="a precio de costo"
          />
        </div>
      )}

      {/* Table */}
      {isError ? (
        <p className="rounded-lg border border-vk-danger/20 bg-vk-danger-bg px-4 py-3 text-sm text-vk-danger">
          Error al cargar los productos. Recargá la página.
        </p>
      ) : !isLoading && filtered.length === 0 ? (
        <EmptyState
          title={stockFilter === "all" ? "Sin productos cargados" : "Sin productos con ese estado"}
          description={
            stockFilter === "all"
              ? "Agregá productos usando el chat."
              : "Cambiá el filtro para ver otros productos."
          }
          action={
            stockFilter === "all"
              ? { label: "Ir al chat", href: "/chat" }
              : undefined
          }
        />
      ) : (
        <Table columns={COLUMNS} data={tableData as Record<string, unknown>[]} />
      )}
    </PageWrapper>
  );
}
