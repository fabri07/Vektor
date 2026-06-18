"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "next/navigation";
import { Pencil, Trash2 } from "lucide-react";
import { PageWrapper } from "@/components/layout/PageWrapper";
import { ManualEntryLauncher } from "@/features/ingestion/ManualEntryLauncher";
import { StatCard } from "@/components/ui/StatCard";
import { SmartTable } from "@/components/ui/SmartTable";
import { Badge } from "@/components/ui/Badge";
import { EmptyState } from "@/components/ui/EmptyState";
import { Modal } from "@/components/ui/Modal";
import {
  productsService,
  type ProductCategoryOption,
  type ProductResponse,
} from "@/services/products.service";
import { fieldDefinitionsService } from "@/services/fieldDefinitions.service";
import { buildCustomFieldColumns } from "@/lib/customFields";
import { formatDateTime, toDatetimeLocal } from "@/lib/datetime";
import { useToastStore } from "@/stores/toastStore";

type StockFilter = "all" | "in_stock" | "low_stock" | "out_of_stock";

function formatARS(value: number): string {
  return new Intl.NumberFormat("es-AR", {
    style: "currency",
    currency: "ARS",
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  }).format(value);
}

function getStockBadge(product: ProductResponse) {
  const status = product.stock_status ?? (
    product.stock_units === 0 ? "out_of_stock" : product.is_low_stock ? "low_stock" : "in_stock"
  );
  if (status === "out_of_stock") return <Badge variant="danger">Sin stock</Badge>;
  if (status === "low_stock") return <Badge variant="warning">Pocas unidades</Badge>;
  return <Badge variant="success">En stock</Badge>;
}

function getStockCsvLabel(product: ProductResponse): string {
  const status = product.stock_status ?? (
    product.stock_units === 0 ? "out_of_stock" : product.is_low_stock ? "low_stock" : "in_stock"
  );
  if (status === "out_of_stock") return "Sin stock";
  if (status === "low_stock") return "Pocas unidades";
  return "En stock";
}

function stockSort(a: ProductResponse, b: ProductResponse): number {
  // out_of_stock → low_stock → in_stock
  const rank = (p: ProductResponse) => {
    const s = p.stock_status ?? (p.stock_units === 0 ? "out_of_stock" : p.is_low_stock ? "low_stock" : "in_stock");
    return s === "out_of_stock" ? 0 : s === "low_stock" ? 1 : 2;
  };
  return rank(a) - rank(b);
}

const STOCK_FILTER_OPTIONS: { value: StockFilter; label: string }[] = [
  { value: "all", label: "Todos" },
  { value: "in_stock", label: "En stock" },
  { value: "low_stock", label: "Pocas unidades" },
  { value: "out_of_stock", label: "Sin stock" },
];

/** Label visible de la categoría: código canónico → label del catálogo;
 * OTHER con nombre custom → ese nombre; sin categoría → "—". */
function categoryDisplay(
  product: ProductResponse,
  catalogLabels: Record<string, string>,
): string {
  const cat = (product.category ?? "").trim();
  if (!cat) return "—";
  if (cat === "OTHER") {
    const label = product.custom_fields?.category_label;
    if (typeof label === "string" && label.trim()) return label.trim();
  }
  return catalogLabels[cat] ?? cat;
}

const COLUMNS = [
  {
    key: "name",
    header: "Producto",
    hideable: true,
    render: (v: unknown, row: Record<string, unknown>) => (
      <span className="flex items-center gap-2">
        <span className="font-medium text-vk-text-primary">{String(v)}</span>
        {/* FASE 3 (B2): producto auto-creado por import al que le faltan precio/costo. */}
        {(row as unknown as ProductResponse).requires_completion ? (
          <Badge variant="warning">Completar datos</Badge>
        ) : null}
      </span>
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
    csvValue: (_: unknown, row: Record<string, unknown>) =>
      getStockCsvLabel(row as unknown as ProductResponse),
  },
  {
    key: "_acquired",
    header: "Fecha de alta",
    hideable: true,
    defaultVisible: false,
    render: (_: unknown, row: Record<string, unknown>) =>
      formatDateTime(row.acquired_at ?? row.created_at),
    csvValue: (_: unknown, row: Record<string, unknown>) =>
      formatDateTime(row.acquired_at ?? row.created_at),
  },
];

export default function ProductsPage() {
  const searchParams = useSearchParams();
  const rawFilter = searchParams.get("stock");
  // Acepta tanto los valores nuevos como los aliases viejos (ok→in_stock, low→low_stock, out→out_of_stock)
  const resolveFilter = (v: string | null): StockFilter => {
    if (v === "ok" || v === "in_stock") return "in_stock";
    if (v === "low" || v === "low_stock") return "low_stock";
    if (v === "out" || v === "out_of_stock") return "out_of_stock";
    return "all";
  };
  const [stockFilter, setStockFilter] = useState<StockFilter>(resolveFilter(rawFilter));
  const [categoryFilter, setCategoryFilter] = useState("all");
  // CTA desde el resumen económico (Balance): ?filter=requires_completion
  // muestra solo productos auto-creados por import sin costo/precio cargado.
  const requiresCompletionOnly = searchParams.get("filter") === "requires_completion";
  const [editing, setEditing] = useState<ProductResponse | null>(null);
  const queryClient = useQueryClient();
  const toast = useToastStore((s) => s.add);

  const { data: products = [], isLoading, isError } = useQuery({
    queryKey: ["products-list"],
    queryFn: () => productsService.getAllProducts({ is_active: true }),
    staleTime: 2 * 60 * 1000,
  });

  // FASE E: catálogo de categorías del vertical del tenant.
  const { data: categories = [] } = useQuery({
    queryKey: ["product-categories"],
    queryFn: () => productsService.getCategories(),
    staleTime: 30 * 60 * 1000,
  });
  const catalogLabels = Object.fromEntries(categories.map((c) => [c.code, c.label]));

  const { data: fieldDefs = [] } = useQuery({
    queryKey: ["field-definitions", "product"],
    queryFn: () => fieldDefinitionsService.getAll("product"),
    staleTime: 5 * 60 * 1000,
  });
  const categoryColumn = {
    key: "category",
    header: "Categoría",
    hideable: true,
    render: (_: unknown, row: Record<string, unknown>) =>
      categoryDisplay(row as unknown as ProductResponse, catalogLabels),
    csvValue: (_: unknown, row: Record<string, unknown>) =>
      categoryDisplay(row as unknown as ProductResponse, catalogLabels),
  };
  const columns = [
    ...COLUMNS.slice(0, 2),
    categoryColumn,
    ...COLUMNS.slice(2),
    ...buildCustomFieldColumns<Record<string, unknown>>(fieldDefs),
  ];

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
        // null = no configurado (usa default 5 del servidor); 0 = umbral explícito
        low_stock_threshold_units: payload.low_stock_threshold_units == null ? null : Number(payload.low_stock_threshold_units),
        acquired_at: payload.acquired_at ?? null,
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

  // Apply filters: stock_status del backend (fallback local) + categoría canónica
  const filtered = products.filter((p) => {
    const status = p.stock_status ?? (
      p.stock_units === 0 ? "out_of_stock" : p.is_low_stock ? "low_stock" : "in_stock"
    );
    if (requiresCompletionOnly && p.requires_completion !== true) return false;
    if (stockFilter !== "all" && status !== stockFilter) return false;
    if (categoryFilter === "none") return !(p.category ?? "").trim();
    if (categoryFilter !== "all" && p.category !== categoryFilter) return false;
    return true;
  });

  // Add _status key for the table (unused by render, just for key lookup)
  const tableData = [...filtered]
    .sort(stockSort)
    .map((p) => ({ ...p, _status: null }));

  return (
    <PageWrapper title="Productos" actions={<ManualEntryLauncher />}>
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
        <label className="ml-3 text-sm text-vk-text-muted">Categoría:</label>
        <select
          value={categoryFilter}
          onChange={(e) => setCategoryFilter(e.target.value)}
          className="rounded-lg border border-vk-border-w bg-vk-surface-w px-3 py-1.5 text-sm text-vk-text-primary focus:outline-none focus:ring-2 focus:ring-vk-blue/20"
        >
          <option value="all">Todas</option>
          {categories.map((c) => (
            <option key={c.code} value={c.code}>
              {c.label}
            </option>
          ))}
          <option value="none">Sin categoría</option>
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
          title={
            requiresCompletionOnly
              ? "No hay productos con costos pendientes"
              : stockFilter === "all"
                ? "Sin productos cargados"
                : "Sin productos con ese estado"
          }
          description={
            requiresCompletionOnly
              ? "Todos tus productos tienen costo cargado."
              : stockFilter === "all"
                ? "Agregá productos usando el chat."
                : "Cambiá el filtro para ver otros productos."
          }
          action={
            requiresCompletionOnly
              ? { label: "Ver todos los productos", href: "/products" }
              : stockFilter === "all"
                ? { label: "Ir al chat", href: "/chat" }
                : undefined
          }
        />
      ) : (
        <SmartTable
          columns={columns}
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
        categories={categories}
        saving={updateMutation.isPending}
        onClose={() => setEditing(null)}
        onSave={(product) => updateMutation.mutate(product)}
      />
    </PageWrapper>
  );
}

function ProductEditModal({
  product,
  categories,
  saving,
  onClose,
  onSave,
}: {
  product: ProductResponse | null;
  categories: ProductCategoryOption[];
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
          <label className="grid gap-1 text-sm text-vk-text-secondary">Categoría
            <select
              className="rounded border border-vk-border-w px-3 py-2"
              value={form.category ?? ""}
              onChange={(e) => set("category", e.target.value || null)}
            >
              <option value="">Sin categoría</option>
              {categories.map((c) => (
                <option key={c.code} value={c.code}>
                  {c.label}
                </option>
              ))}
              {/* Valor actual fuera del catálogo (legacy/raw): conservarlo como opción */}
              {form.category && !categories.some((c) => c.code === form.category) ? (
                <option value={form.category}>{form.category}</option>
              ) : null}
            </select>
          </label>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <label className="grid gap-1 text-sm text-vk-text-secondary">Precio<input className="rounded border border-vk-border-w px-3 py-2" type="number" min={0} step="0.01" value={form.sale_price_ars} onChange={(e) => set("sale_price_ars", Number(e.target.value))} /></label>
          <label className="grid gap-1 text-sm text-vk-text-secondary">Costo<input className="rounded border border-vk-border-w px-3 py-2" type="number" min={0} step="0.01" value={form.unit_cost_ars ?? ""} onChange={(e) => set("unit_cost_ars", e.target.value ? Number(e.target.value) : null)} /></label>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <label className="grid gap-1 text-sm text-vk-text-secondary">Stock<input className="rounded border border-vk-border-w px-3 py-2" type="number" min={0} value={form.stock_units} onChange={(e) => set("stock_units", Number(e.target.value))} /></label>
          <label className="grid gap-1 text-sm text-vk-text-secondary">Umbral mínimo<input className="rounded border border-vk-border-w px-3 py-2" type="number" min={0} placeholder="5 (default)" value={form.low_stock_threshold_units ?? ""} onChange={(e) => set("low_stock_threshold_units", e.target.value === "" ? null : Number(e.target.value))} /></label>
        </div>
        <label className="grid gap-1 text-sm text-vk-text-secondary">Descripción<input className="rounded border border-vk-border-w px-3 py-2" value={form.description ?? ""} onChange={(e) => set("description", e.target.value || null)} /></label>
        <label className="grid gap-1 text-sm text-vk-text-secondary">Fecha de alta<input className="rounded border border-vk-border-w px-3 py-2" type="datetime-local" value={form.acquired_at ? toDatetimeLocal(form.acquired_at) : ""} onChange={(e) => set("acquired_at", e.target.value || null)} /></label>
        <div className="flex justify-end gap-2 pt-2">
          <button type="button" onClick={onClose} className="rounded border border-vk-border-w px-4 py-2 text-sm">Cancelar</button>
          <button type="submit" disabled={saving} className="rounded bg-vk-blue px-4 py-2 text-sm font-medium text-white disabled:opacity-50">{saving ? "Guardando..." : "Guardar"}</button>
        </div>
      </form>
    </Modal>
  );
}
