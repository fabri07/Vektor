"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "next/navigation";
import { Pencil, Trash2 } from "lucide-react";
import { PageWrapper } from "@/components/layout/PageWrapper";
import { StatCard } from "@/components/ui/StatCard";
import { SmartTable } from "@/components/ui/SmartTable";
import { Badge } from "@/components/ui/Badge";
import { EmptyState } from "@/components/ui/EmptyState";
import { Modal } from "@/components/ui/Modal";
import { productsService, type ProductResponse } from "@/services/products.service";
import { useToastStore } from "@/stores/toastStore";

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
    hideable: true,
    render: (v: unknown) => (
      <span className="font-medium text-vk-text-primary">{String(v)}</span>
    ),
    csvValue: (v: unknown) => String(v ?? ""),
  },
  {
    key: "sku",
    header: "SKU",
    hideable: true,
    defaultVisible: false,
    render: (v: unknown) => String(v ?? "").trim() || "—",
    csvValue: (v: unknown) => String(v ?? "").trim(),
  },
  {
    key: "category",
    header: "Categoría",
    hideable: true,
    render: (v: unknown) => String(v ?? "").trim() || "—",
    csvValue: (v: unknown) => String(v ?? "").trim(),
  },
  {
    key: "stock_units",
    header: "Stock",
    hideable: true,
    render: (v: unknown) => String(v),
    csvValue: (v: unknown) => String(v ?? ""),
  },
  {
    key: "sale_price_ars",
    header: "Precio",
    hideable: true,
    render: (v: unknown) => formatARS(Number(v)),
    csvValue: (v: unknown) => String(Number(v ?? 0)),
  },
  {
    key: "margin_pct",
    header: "Margen",
    hideable: true,
    defaultVisible: false,
    render: (v: unknown) =>
      v != null ? `${Number(v).toFixed(1)}%` : "—",
    csvValue: (v: unknown) => (v != null ? `${Number(v).toFixed(1)}%` : ""),
  },
  {
    key: "_status",
    header: "Estado",
    hideable: true,
    render: (_: unknown, row: Record<string, unknown>) =>
      getStockBadge(row as unknown as ProductResponse),
    csvValue: (_: unknown, row: Record<string, unknown>) => {
      const product = row as unknown as ProductResponse;
      if (product.stock_units === 0) return "Sin stock";
      if (product.is_low_stock) return "Stock bajo";
      return "OK";
    },
  },
];

export default function ProductsPage() {
  const searchParams = useSearchParams();
  const initialFilter = (searchParams.get("stock") as StockFilter | null) ?? "all";
  const [stockFilter, setStockFilter] = useState<StockFilter>(
    initialFilter === "ok" || initialFilter === "low" || initialFilter === "out" ? initialFilter : "all",
  );
  const [editing, setEditing] = useState<ProductResponse | null>(null);
  const queryClient = useQueryClient();
  const toast = useToastStore((s) => s.add);

  const { data: products = [], isLoading, isError } = useQuery({
    queryKey: ["products-list"],
    queryFn: () => productsService.getAllProducts({ is_active: true }),
    staleTime: 2 * 60 * 1000,
  });

  const updateMutation = useMutation({
    mutationFn: (payload: ProductResponse) =>
      productsService.updateProduct(payload.id, {
        name: payload.name,
        sku: payload.sku,
        description: payload.description,
        category: payload.category,
        sale_price_ars: Number(payload.sale_price_ars),
        unit_cost_ars: payload.unit_cost_ars == null ? null : Number(payload.unit_cost_ars),
        stock_units: Number(payload.stock_units),
        low_stock_threshold_units: Number(payload.low_stock_threshold_units),
      }),
    onSuccess: async () => {
      setEditing(null);
      toast("Producto actualizado.", "success");
      await queryClient.invalidateQueries({ queryKey: ["products-list"] });
    },
    onError: () => toast("No se pudo actualizar el producto.", "error"),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => productsService.deleteProduct(id),
    onSuccess: async () => {
      toast("Producto desactivado.", "success");
      await queryClient.invalidateQueries({ queryKey: ["products-list"] });
    },
    onError: () => toast("No se pudo desactivar el producto.", "error"),
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
        <SmartTable
          columns={COLUMNS}
          data={tableData as Record<string, unknown>[]}
          exportFilename="vektor-productos"
          renderActions={(row) => {
            const product = row as unknown as ProductResponse;
            return (
              <div className="flex items-center gap-1">
                <button type="button" title="Editar" aria-label="Editar producto" onClick={() => setEditing(product)} className="inline-flex h-8 w-8 items-center justify-center rounded border border-vk-border-w text-vk-text-secondary transition-colors hover:bg-vk-bg-light hover:text-vk-text-primary">
                  <Pencil className="h-4 w-4" />
                </button>
                <button
                  type="button"
                  title="Desactivar"
                  aria-label="Desactivar producto"
                  disabled={deleteMutation.isPending}
                  onClick={() => {
                    if (confirm("¿Desactivar este producto?")) deleteMutation.mutate(product.id);
                  }}
                  className="inline-flex h-8 w-8 items-center justify-center rounded border border-vk-danger/30 text-vk-danger transition-colors hover:bg-vk-danger-bg disabled:opacity-50"
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>
            );
          }}
        />
      )}
      <ProductEditModal
        product={editing}
        saving={updateMutation.isPending}
        onClose={() => setEditing(null)}
        onSave={(product) => updateMutation.mutate(product)}
      />
    </PageWrapper>
  );
}

function ProductEditModal({
  product,
  saving,
  onClose,
  onSave,
}: {
  product: ProductResponse | null;
  saving: boolean;
  onClose: () => void;
  onSave: (product: ProductResponse) => void;
}) {
  const [form, setForm] = useState<ProductResponse | null>(product);
  useEffect(() => {
    setForm(product);
  }, [product]);
  if (!form) return null;
  const set = (key: keyof ProductResponse, value: string | number | null) => {
    setForm((prev) => (prev ? { ...prev, [key]: value } : prev));
  };
  return (
    <Modal isOpen={!!product} onClose={onClose} title="Editar producto" size="lg">
      <form className="grid gap-4" onSubmit={(event) => { event.preventDefault(); onSave(form); }}>
        <label className="grid gap-1 text-sm text-vk-text-secondary">Producto<input className="rounded border border-vk-border-w px-3 py-2" value={form.name} onChange={(e) => set("name", e.target.value)} /></label>
        <div className="grid grid-cols-2 gap-3">
          <label className="grid gap-1 text-sm text-vk-text-secondary">SKU<input className="rounded border border-vk-border-w px-3 py-2" value={form.sku ?? ""} onChange={(e) => set("sku", e.target.value || null)} /></label>
          <label className="grid gap-1 text-sm text-vk-text-secondary">Categoría<input className="rounded border border-vk-border-w px-3 py-2" value={form.category ?? ""} onChange={(e) => set("category", e.target.value || null)} /></label>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <label className="grid gap-1 text-sm text-vk-text-secondary">Precio<input className="rounded border border-vk-border-w px-3 py-2" type="number" min={0} step="0.01" value={form.sale_price_ars} onChange={(e) => set("sale_price_ars", Number(e.target.value))} /></label>
          <label className="grid gap-1 text-sm text-vk-text-secondary">Costo<input className="rounded border border-vk-border-w px-3 py-2" type="number" min={0} step="0.01" value={form.unit_cost_ars ?? ""} onChange={(e) => set("unit_cost_ars", e.target.value ? Number(e.target.value) : null)} /></label>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <label className="grid gap-1 text-sm text-vk-text-secondary">Stock<input className="rounded border border-vk-border-w px-3 py-2" type="number" min={0} value={form.stock_units} onChange={(e) => set("stock_units", Number(e.target.value))} /></label>
          <label className="grid gap-1 text-sm text-vk-text-secondary">Umbral<input className="rounded border border-vk-border-w px-3 py-2" type="number" min={0} value={form.low_stock_threshold_units} onChange={(e) => set("low_stock_threshold_units", Number(e.target.value))} /></label>
        </div>
        <label className="grid gap-1 text-sm text-vk-text-secondary">Descripción<input className="rounded border border-vk-border-w px-3 py-2" value={form.description ?? ""} onChange={(e) => set("description", e.target.value || null)} /></label>
        <div className="flex justify-end gap-2 pt-2">
          <button type="button" onClick={onClose} className="rounded border border-vk-border-w px-4 py-2 text-sm">Cancelar</button>
          <button type="submit" disabled={saving} className="rounded bg-vk-blue px-4 py-2 text-sm font-medium text-white disabled:opacity-50">{saving ? "Guardando..." : "Guardar"}</button>
        </div>
      </form>
    </Modal>
  );
}
