"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "next/navigation";
import { Pencil, Trash2, Eye } from "lucide-react";
import { PageWrapper } from "@/components/layout/PageWrapper";
import { ManualEntryLauncher } from "@/features/ingestion/ManualEntryLauncher";
import { LabelsModal } from "@/features/products/LabelsModal";
import { StockDiscrepanciesPanel } from "@/features/products/StockDiscrepanciesPanel";
import { StatCard } from "@/components/ui/StatCard";
import { SmartTable } from "@/components/ui/SmartTable";
import { Badge } from "@/components/ui/Badge";
import { EmptyState } from "@/components/ui/EmptyState";
import { Modal } from "@/components/ui/Modal";
import {
  productsService,
  PRODUCTS_MAX_ACCUMULATED,
  type ProductCategoryOption,
  type ProductResponse,
} from "@/services/products.service";
import { AddColumnButton } from "@/features/customFields/AddColumnButton";
import { ExportAllFieldsButton } from "@/features/customFields/ExportAllFieldsButton";
import { useSaveCustomField } from "@/features/customFields/useSaveCustomField";
import { useBusinessFieldColumns } from "@/features/customFields/useBusinessFieldColumns";
import { AllDataModal } from "@/features/customFields/AllDataModal";
import { formatDateTime, toDatetimeLocal } from "@/lib/datetime";
import { UNIDADES_DE_VENTA, unidadDeVentaDe } from "@/features/products/saleUnitOptions";
import { aUnidadesBase, enUnidadDeVenta, valorDeStock } from "@/lib/pos/saleUnit";
import { posErrorInfo } from "@/lib/pos/errors";
import { useToastStore } from "@/stores/toastStore";
import { useAuthStore } from "@/stores/authStore";

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

// F6-B4: badge de vencimiento a nivel producto (informativo). Solo se muestra si
// venció o está por vencer; "ok"/sin fecha no renderiza nada.
function getExpiryBadge(product: ProductResponse) {
  if (product.expiry_status === "expired") return <Badge variant="danger">Vencido</Badge>;
  if (product.expiry_status === "expiring_soon")
    return <Badge variant="warning">Por vencer</Badge>;
  return null;
}

function getExpiryCsvSuffix(product: ProductResponse): string {
  if (product.expiry_status === "expired") return " · Vencido";
  if (product.expiry_status === "expiring_soon") return " · Por vencer";
  return "";
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
    // Visible por default: cuando el archivo no trae código, el generado por
    // Véktor es lo único con lo que el usuario puede referirse a un producto.
    // Estaba oculta, que es media razón por la que nunca se vio.
    defaultVisible: true,
    render: (v: unknown, row: Record<string, unknown>) => {
      const propio = (row as unknown as ProductResponse).internal_sku;
      const delArchivo = String(v ?? "").trim();
      if (delArchivo) return delArchivo;
      // El chip distingue "este código te lo dio tu proveedor" de "este lo
      // inventamos nosotros": sin eso, alguien lo buscaría en el catálogo del
      // proveedor y no lo encontraría.
      return propio ? (
        <span className="flex items-center gap-2">
          <span>{propio}</span>
          <Badge>Generado</Badge>
        </span>
      ) : (
        "—"
      );
    },
    // El CSV exporta el mismo valor que se ve, sin el chip: una exportación que
    // dejara la celda vacía cuando la pantalla muestra un código estaría
    // mintiendo sobre lo que el usuario tiene.
    csvValue: (v: unknown, row: Record<string, unknown>) =>
      String(v ?? "").trim() ||
      String((row as unknown as ProductResponse).internal_sku ?? ""),
  },
  {
    key: "stock_units",
    header: "Stock",
    hideable: true,
    // Con su unidad: 5000 de un producto por kilo son "5,000 kg", no 5000.
    render: (v: unknown, row: Record<string, unknown>) =>
      String((row as unknown as ProductResponse).stock_display ?? v),
    csvValue: (v: unknown, row: Record<string, unknown>) =>
      String((row as unknown as ProductResponse).stock_display ?? v ?? ""),
  },
  {
    key: "sale_price_ars",
    header: "Precio",
    hideable: true,
    render: (v: unknown) => formatARS(Number(v)),
    csvValue: (v: unknown) => String(Number(v ?? 0)),
  },
  {
    key: "unit_cost_ars",
    header: "Costo unitario",
    hideable: true,
    render: (v: unknown) => (v != null ? formatARS(Number(v)) : "—"),
    csvValue: (v: unknown) => (v != null ? String(Number(v)) : ""),
  },
  {
    key: "list_price_ars",
    header: "Precio de lista (sugerido)",
    hideable: true,
    // Oculta por default: en la práctica la trae una minoría de productos
    // (informativo, no entra al margen) — no vale la pena mostrarla vacía en
    // el 90%+ de las filas, pero el dato no se pierde: sigue seleccionable.
    defaultVisible: false,
    render: (v: unknown) => (v != null ? formatARS(Number(v)) : "—"),
    csvValue: (v: unknown) => (v != null ? String(Number(v)) : ""),
  },
  {
    key: "_purchase_base_cost",
    fieldId: "product:custom_fields.purchase_base_cost",
    header: "Precio de compra (costo base, sin envío)",
    hideable: true,
    // Auxiliar del costo (F-H6.d): el archivo lo trae por separado del costo
    // final (`unit_cost_ars`, que ya puede incluir el flete), preservado tal
    // cual para no perder de dónde salió. Solo lectura: no es lo que usa el
    // margen, así que no se edita desde acá.
    render: (_: unknown, row: Record<string, unknown>) => {
      const raw = (row.custom_fields as Record<string, unknown> | undefined)?.purchase_base_cost;
      return raw != null && raw !== "" ? formatARS(Number(raw)) : "—";
    },
    csvValue: (_: unknown, row: Record<string, unknown>) => {
      const raw = (row.custom_fields as Record<string, unknown> | undefined)?.purchase_base_cost;
      return raw != null && raw !== "" ? String(Number(raw)) : "";
    },
  },
  {
    key: "_shipping_percentage",
    fieldId: "product:custom_fields.shipping_percentage",
    header: "% de envío sobre el costo base",
    hideable: true,
    // Sparse en la práctica (la mayoría de los archivos no la traen) — oculta
    // por default, disponible en el selector de columnas.
    defaultVisible: false,
    render: (_: unknown, row: Record<string, unknown>) => {
      const raw = (row.custom_fields as Record<string, unknown> | undefined)?.shipping_percentage;
      return raw != null && raw !== "" ? String(raw) : "—";
    },
    csvValue: (_: unknown, row: Record<string, unknown>) => {
      const raw = (row.custom_fields as Record<string, unknown> | undefined)?.shipping_percentage;
      return raw != null && raw !== "" ? String(raw) : "";
    },
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
    render: (_: unknown, row: Record<string, unknown>) => {
      const p = row as unknown as ProductResponse;
      return (
        <span className="flex flex-wrap items-center gap-1">
          {getStockBadge(p)}
          {getExpiryBadge(p)}
        </span>
      );
    },
    csvValue: (_: unknown, row: Record<string, unknown>) => {
      const p = row as unknown as ProductResponse;
      return getStockCsvLabel(p) + getExpiryCsvSuffix(p);
    },
  },
  {
    key: "_acquired",
    fieldId: "product:acquired_at",
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
  const [labelsOpen, setLabelsOpen] = useState(false);
  // CTA desde el resumen económico (Balance): ?filter=requires_completion
  // muestra solo productos auto-creados por import sin costo/precio cargado.
  const requiresCompletionOnly = searchParams.get("filter") === "requires_completion";
  const [editing, setEditing] = useState<ProductResponse | null>(null);
  // Cambio 2: fila para la que se abrió "Todos los datos" (null = cerrado).
  const [viewingAllData, setViewingAllData] = useState<ProductResponse | null>(null);
  const queryClient = useQueryClient();
  const toast = useToastStore((s) => s.add);
  const user = useAuthStore((s) => s.user);

  const { data: products = [], isLoading, isError } = useQuery({
    queryKey: ["products-list"],
    queryFn: () => productsService.getAllProducts({ is_active: true }),
    staleTime: 2 * 60 * 1000,
  });

  // Corrección C4 (revisión externa 2026-08-19): `getAllProducts` acumula
  // hasta `PRODUCTS_MAX_ACCUMULATED` productos y corta en silencio — sin este
  // chequeo, un catálogo más grande que el techo se mostraba incompleto sin
  // ningún aviso (no-invention: nunca mostrar una lista parcial como si fuera
  // el catálogo entero). Solo se pide el total real cuando hace falta.
  const possiblyTruncated = products.length >= PRODUCTS_MAX_ACCUMULATED;
  const { data: totalActiveCount } = useQuery({
    queryKey: ["products-count", "active"],
    queryFn: () => productsService.countProducts({ is_active: true }),
    enabled: possiblyTruncated,
    staleTime: 2 * 60 * 1000,
  });

  // FASE E: catálogo de categorías del vertical del tenant.
  const { data: categories = [] } = useQuery({
    queryKey: ["product-categories"],
    queryFn: () => productsService.getCategories(),
    staleTime: 30 * 60 * 1000,
  });
  const catalogLabels = Object.fromEntries(categories.map((c) => [c.code, c.label]));

  // B·3: la categoría inferida es SÓLO sugerencia. Aceptarla es el mismo PATCH
  // de producto que usa la edición, mandando nada más que la categoría.
  const acceptSuggestionMutation = useMutation({
    mutationFn: (payload: { id: string; category: string }) =>
      productsService.updateProduct(payload.id, { category: payload.category }),
    onSuccess: async () => {
      toast("Categoría asignada.", "success");
      await queryClient.invalidateQueries({ queryKey: ["products-list"] });
    },
    onError: () => toast("No se pudo asignar la categoría.", "error"),
  });

  const categoryColumn = {
    key: "category",
    header: "Categoría",
    hideable: true,
    render: (_: unknown, row: Record<string, unknown>) => {
      const product = row as unknown as ProductResponse;
      const suggestion = product.category_suggestion;
      if ((product.category ?? "").trim() || !suggestion) {
        return categoryDisplay(product, catalogLabels);
      }
      return (
        <span className="flex items-center gap-2">
          <span className="text-vk-text-secondary">Sugerida: {suggestion.label}</span>
          <button
            type="button"
            aria-label={`Aceptar categoría sugerida ${suggestion.label} para ${product.name}`}
            disabled={acceptSuggestionMutation.isPending}
            onClick={(e) => {
              e.stopPropagation();
              acceptSuggestionMutation.mutate({ id: product.id, category: suggestion.code });
            }}
            className="rounded-md border border-vk-border-w px-2 py-0.5 text-xs text-vk-text-secondary transition-colors hover:bg-vk-bg-light hover:text-vk-text-primary disabled:opacity-50"
          >
            Aceptar
          </button>
        </span>
      );
    },
    csvValue: (_: unknown, row: Record<string, unknown>) =>
      categoryDisplay(row as unknown as ProductResponse, catalogLabels),
  };
  const saveProductCustomField = useSaveCustomField({
    listKey: ["products-list"],
    update: (id, custom_fields) => productsService.updateProduct(id, { custom_fields }),
  });

  const columns = useBusinessFieldColumns<Record<string, unknown>>({
    entityType: "product",
    existingColumns: [
      ...COLUMNS.slice(0, 2),
      categoryColumn,
      ...COLUMNS.slice(2),
    ],
    onSaveCustomField: saveProductCustomField,
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
        list_price_ars: payload.list_price_ars == null ? null : Number(payload.list_price_ars),
        stock_units: Number(payload.stock_units),
        // Van juntos (el servidor lo exige). Mandarlos sin cambio no cambia
        // nada; cambiarlos en un producto con historia da 409.
        sale_unit: payload.sale_unit ?? "unit",
        base_units_per_sale_unit: payload.base_units_per_sale_unit ?? 1,
        // null = no configurado (usa default 5 del servidor); 0 = umbral explícito
        low_stock_threshold_units: payload.low_stock_threshold_units == null ? null : Number(payload.low_stock_threshold_units),
        acquired_at: payload.acquired_at ?? null,
      }),
    onSuccess: async () => {
      setEditing(null);
      toast("Producto actualizado.", "success");
      await queryClient.invalidateQueries({ queryKey: ["products-list"] });
    },
    onError: (e) =>
      toast(
        errorCode(e) === "SALE_UNIT_LOCKED"
          ? "No se puede cambiar la unidad de un producto con stock, ventas o movimientos."
          : "No se pudo actualizar el producto.",
        "error",
      ),
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
  // Por el helper: precio y costo son por unidad de VENTA y el stock está en
  // unidades base, así que un producto en gramos divide por su factor.
  const inventoryValue = products.reduce(
    (s, p) =>
      s + valorDeStock(p.stock_units, p.base_units_per_sale_unit, p.unit_cost_ars ?? 0),
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
    <PageWrapper
      title="Productos"
      actions={
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setLabelsOpen(true)}
            className="h-9 rounded-lg border border-vk-border-w px-3 text-sm text-vk-text-secondary transition-colors hover:bg-vk-bg-light hover:text-vk-text-primary"
          >
            Imprimir etiquetas
          </button>
          <ManualEntryLauncher />
        </div>
      }
    >
      <LabelsModal isOpen={labelsOpen} onClose={() => setLabelsOpen(false)} />
      {/* Corrección C4 (revisión externa 2026-08-19): sin este aviso, un
          catálogo más grande que PRODUCTS_MAX_ACCUMULATED se mostraba
          incompleto sin decirlo — no-invention: nunca una lista parcial
          disfrazada de catálogo completo. */}
      {possiblyTruncated && (
        <div
          role="status"
          className="rounded-lg border border-vk-warning/40 bg-vk-warning-bg px-4 py-3 text-sm text-vk-warning"
        >
          {totalActiveCount !== undefined
            ? `Este catálogo tiene ${totalActiveCount} productos activos`
            : "Este catálogo tiene más de " + PRODUCTS_MAX_ACCUMULATED + " productos activos"}{" "}
          — mostramos los primeros {PRODUCTS_MAX_ACCUMULATED}. Si necesitás ver el resto,
          contactanos.
        </div>
      )}

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

      {/* Sólo aparece si hay diferencias abiertas (B11). */}
      <StockDiscrepanciesPanel />

      {/* Table */}
      {isError ? (
        <p className="rounded-lg border border-vk-danger/20 bg-vk-danger-bg px-4 py-3 text-sm text-vk-danger">
          Error al cargar los productos. Recargá la página.
        </p>
      ) : isLoading ? (
        // Sin esto la tabla vacía decía «No hay datos para mostrar» mientras
        // cargaba, y parecía que el catálogo se había borrado.
        <div className="space-y-2" aria-busy="true" aria-label="Cargando productos">
          {[...Array<number>(6)].map((_, i) => (
            <div
              key={i}
              className="h-10 animate-pulse rounded-lg border border-vk-border-w bg-vk-surface-w"
            />
          ))}
        </div>
      ) : filtered.length === 0 ? (
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
          toolbarActions={
            <>
              <AddColumnButton entityType="product" entityLabel="Productos" />
              <ExportAllFieldsButton entityType="product" entityLabel="Productos" />
            </>
          }
          // Cambio 2: preferencias de columnas versionadas por tenant+usuario+
          // sección. Sin usuario logueado (no debería pasar en una ruta
          // protegida) queda sin persistir — mismo comportamiento de siempre.
          storageKey={user ? `${user.tenant_id}:${user.id}:products` : undefined}
          renderActions={(row) => {
            const product = row as unknown as ProductResponse;
            return (
              <div className="flex items-center gap-1">
                <button
                  type="button"
                  title="Ver todos los datos"
                  aria-label="Ver todos los datos"
                  onClick={() => setViewingAllData(product)}
                  className="inline-flex h-8 w-8 items-center justify-center rounded border border-vk-border-w text-vk-text-secondary transition-colors hover:bg-vk-bg-light hover:text-vk-text-primary"
                >
                  <Eye className="h-4 w-4" />
                </button>
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
      {viewingAllData && (
        <AllDataModal
          entityType="product"
          title={viewingAllData.name}
          row={viewingAllData}
          isOpen={true}
          onClose={() => setViewingAllData(null)}
        />
      )}
    </PageWrapper>
  );
}

function errorCode(e: unknown): string | null {
  return posErrorInfo(e).code;
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
  // Stock y umbral se escriben en la unidad de VENTA (kg, L) y se guardan en
  // unidades base: el texto vive aparte para poder tipear "0,750".
  const [stockText, setStockText] = useState("");
  const [umbralText, setUmbralText] = useState("");
  useEffect(() => {
    setForm(product);
    if (product) {
      const f = product.base_units_per_sale_unit ?? 1;
      setStockText(enUnidadDeVenta(product.stock_units, f));
      setUmbralText(
        product.low_stock_threshold_units == null
          ? ""
          : enUnidadDeVenta(product.low_stock_threshold_units, f),
      );
    }
  }, [product]);
  if (!form) return null;
  const set = (key: keyof ProductResponse, value: string | number | null) => {
    setForm((prev) => (prev ? { ...prev, [key]: value } : prev));
  };
  const unidad = unidadDeVentaDe(form.sale_unit, form.base_units_per_sale_unit);
  const factor = unidad.factor;
  const cantidad = (texto: string): number | null =>
    texto.trim() === "" || texto.trim() === "0" ? 0 : aUnidadesBase(texto, factor);
  const stockBase = cantidad(stockText);
  const umbralBase = umbralText.trim() === "" ? null : cantidad(umbralText);
  const invalido = stockBase === null || (umbralText.trim() !== "" && umbralBase === null);
  return (
    <Modal isOpen={!!product} onClose={onClose} title="Editar producto" size="lg">
      <form
        className="grid gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (invalido) return;
          onSave({ ...form, stock_units: stockBase ?? 0, low_stock_threshold_units: umbralBase });
        }}
      >
        <label className="grid gap-1 text-sm text-vektor-body">Producto<input className="rounded border border-vk-border-w px-3 py-2" value={form.name} onChange={(e) => set("name", e.target.value)} /></label>
        <div className="grid grid-cols-2 gap-3">
          <label className="grid gap-1 text-sm text-vektor-body">SKU<input className="rounded border border-vk-border-w px-3 py-2" value={form.sku ?? ""} onChange={(e) => set("sku", e.target.value || null)} /></label>
          <label className="grid gap-1 text-sm text-vektor-body">Categoría
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
        <label className="grid gap-1 text-sm text-vektor-body">
          Se vende por
          <select
            className="rounded border border-vk-border-w px-3 py-2"
            value={unidad.key}
            onChange={(e) => {
              const nueva = UNIDADES_DE_VENTA.find((u) => u.key === e.target.value);
              if (!nueva) return;
              setForm((prev) =>
                prev
                  ? { ...prev, sale_unit: nueva.saleUnit, base_units_per_sale_unit: nueva.factor }
                  : prev,
              );
            }}
          >
            {UNIDADES_DE_VENTA.map((u) => (
              <option key={u.key} value={u.key}>
                {u.label}
              </option>
            ))}
          </select>
          <span className="text-xs text-vektor-muted">
            Sólo se puede cambiar en un producto sin stock, ventas ni compras.
          </span>
        </label>
        <div className="grid grid-cols-2 gap-3">
          <label className="grid gap-1 text-sm text-vektor-body">Precio{unidad.sufijo}<input className="rounded border border-vk-border-w px-3 py-2" type="number" min={0} step="0.01" value={form.sale_price_ars} onChange={(e) => set("sale_price_ars", Number(e.target.value))} /></label>
          <label className="grid gap-1 text-sm text-vektor-body">Costo{unidad.sufijo}<input className="rounded border border-vk-border-w px-3 py-2" type="number" min={0} step="0.01" value={form.unit_cost_ars ?? ""} onChange={(e) => set("unit_cost_ars", e.target.value ? Number(e.target.value) : null)} /></label>
        </div>
        <label className="grid gap-1 text-sm text-vektor-body">
          Precio de lista (sugerido)
          <input className="rounded border border-vk-border-w px-3 py-2" type="number" min={0} step="0.01" value={form.list_price_ars ?? ""} onChange={(e) => set("list_price_ars", e.target.value ? Number(e.target.value) : null)} />
        </label>
        <div className="grid grid-cols-2 gap-3">
          <label className="grid gap-1 text-sm text-vektor-body">Stock ({unidad.etiqueta})<input className="rounded border border-vk-border-w px-3 py-2" inputMode="decimal" value={stockText} onChange={(e) => setStockText(e.target.value)} /></label>
          <label className="grid gap-1 text-sm text-vektor-body">Umbral mínimo ({unidad.etiqueta})<input className="rounded border border-vk-border-w px-3 py-2" inputMode="decimal" placeholder="5 (default)" value={umbralText} onChange={(e) => setUmbralText(e.target.value)} /></label>
        </div>
        {invalido ? (
          <p className="text-xs text-vk-danger">
            Revisá el stock y el umbral: {unidad.factor > 1 ? "hasta 3 decimales" : "números enteros"}.
          </p>
        ) : null}
        <label className="grid gap-1 text-sm text-vektor-body">Descripción<input className="rounded border border-vk-border-w px-3 py-2" value={form.description ?? ""} onChange={(e) => set("description", e.target.value || null)} /></label>
        <label className="grid gap-1 text-sm text-vektor-body">Fecha de alta<input className="rounded border border-vk-border-w px-3 py-2" type="datetime-local" value={form.acquired_at ? toDatetimeLocal(form.acquired_at) : ""} onChange={(e) => set("acquired_at", e.target.value || null)} /></label>
        <div className="flex justify-end gap-2 pt-2">
          <button type="button" onClick={onClose} className="rounded border border-vk-border-w px-4 py-2 text-sm">Cancelar</button>
          <button type="submit" disabled={saving || invalido} className="rounded bg-vk-blue px-4 py-2 text-sm font-medium text-white disabled:opacity-50">{saving ? "Guardando..." : "Guardar"}</button>
        </div>
      </form>
    </Modal>
  );
}
