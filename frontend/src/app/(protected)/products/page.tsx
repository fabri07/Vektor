"use client";

import { useEffect, useRef, useState } from "react";
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
  type TratamientoEnvio,
} from "@/services/products.service";
import { AddColumnButton } from "@/features/customFields/AddColumnButton";
import { ExportAllFieldsButton } from "@/features/customFields/ExportAllFieldsButton";
import { useSaveCustomField } from "@/features/customFields/useSaveCustomField";
import { useBusinessFieldColumns } from "@/features/customFields/useBusinessFieldColumns";
import { AllDataModal } from "@/features/customFields/AllDataModal";
import { formatDateTime, toDatetimeLocal } from "@/lib/datetime";
import { UNIDADES_DE_VENTA, unidadDeVentaDe } from "@/features/products/saleUnitOptions";
import {
  COSTO_REQUERIDO_CON_STOCK,
  EnvioSelect,
  QUE_ES_STOCK_INICIAL,
  fechaDeStockInicial,
  hoyLocal,
  nuevaClaveIdempotencia,
} from "@/features/products/costoManual";
import { esPinCancelado } from "@/stores/pinGateStore";
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

/** M·P4: el costo promedio ponderado, como DATO al lado del de reposición.
 *  Sólo existe si el servidor lo manda (cuenta habilitada): sin dato en ningún
 *  producto la columna no aparece. */
const AVG_COST_COLUMN = {
  key: "avg_cost_ars",
  header: "Costo promedio (estimado)",
  hideable: true,
  // Fuera del CSV: el importador lee cualquier «costo …» como unit_cost_ars, y
  // reimportar el archivo pisaría el costo de reposición con el promedio.
  exportable: false,
  render: (v: unknown, row: Record<string, unknown>) => {
    const product = row as unknown as ProductResponse;
    if (product.avg_cost_estado === "sin_medicion" || v == null) {
      return (
        <span className="text-vk-text-secondary" title={product.avg_cost_motivo ?? undefined}>
          {product.avg_cost_estado === "sin_medicion" ? "Sin medir" : "—"}
        </span>
      );
    }
    return (
      <span title={product.avg_cost_motivo ?? undefined}>
        {formatARS(Number(v))}
        {product.avg_cost_estado === "incompleto" && (
          <span className="ml-1 text-xs text-vk-text-secondary">(parcial)</span>
        )}
      </span>
    );
  },
  csvValue: (v: unknown) => (v != null ? String(Number(v)) : ""),
};

function tieneCostoPromedio(products: ProductResponse[]): boolean {
  return products.some((p) => p.avg_cost_estado != null);
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
    // M·P4: es el de la última compra (reposición), el que usan el margen y el
    // valor de stock; el promedio va en su propia columna. El rótulo lo pisa el
    // catálogo del backend (``business_field_catalog``), que dice lo mismo.
    header: "Costo de reposición",
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
    // M·3-2: visible, con el motivo cuando no hay (un costo 0 ya no da 100%).
    defaultVisible: true,
    render: (v: unknown, row: Record<string, unknown>) => {
      if (v != null) {
        // M·3-4: «costo desactualizado» con más de 45 días desde la última compra.
        const texto =
          typeof row.costo_antiguedad_texto === "string" ? row.costo_antiguedad_texto : null;
        return (
          <span title={texto ?? undefined}>
            {`${Number(v).toFixed(1)}%`}
            {row.costo_desactualizado === true && (
              <span className="ml-1 text-xs text-vk-warning">(costo desactualizado: {texto})</span>
            )}
          </span>
        );
      }
      const motivo = typeof row.margin_motivo === "string" ? row.margin_motivo : null;
      return (
        <span className="text-vk-text-muted" title={motivo ?? undefined}>
          —{motivo ? ` (${motivo})` : ""}
        </span>
      );
    },
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
  // B·5d: el 409 OPENING_EXISTS del stock inicial, con sus caminos, en el editor.
  const [errorApertura, setErrorApertura] = useState<ErrorDeApertura | null>(null);
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
          {/* B·3b: si la sugerida no sirve, se elige o crea otra en el MISMO editor. */}
          <button
            type="button"
            aria-label={`Elegir otra categoría para ${product.name}`}
            onClick={(e) => {
              e.stopPropagation();
              setEditing(product);
            }}
            className="rounded-md border border-vk-border-w px-2 py-0.5 text-xs text-vk-text-secondary transition-colors hover:bg-vk-bg-light hover:text-vk-text-primary"
          >
            Otra
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
      ...COLUMNS.slice(2).flatMap((c) =>
        c.key === "unit_cost_ars" && tieneCostoPromedio(products) ? [c, AVG_COST_COLUMN] : [c],
      ),
    ],
    onSaveCustomField: saveProductCustomField,
  });

  const updateMutation = useMutation({
    mutationFn: async ({
      product: payload,
      stock,
      apertura,
    }: {
      product: ProductResponse;
      stock: CambioDeStock | null;
      apertura: AperturaPedida | null;
    }) => {
      const actualizado = await productsService.updateProduct(payload.id, {
        name: payload.name,
        sku: payload.sku,
        description: payload.description,
        category: payload.category,
        sale_price_ars: Number(payload.sale_price_ars),
        unit_cost_ars: payload.unit_cost_ars == null ? null : Number(payload.unit_cost_ars),
        list_price_ars: payload.list_price_ars == null ? null : Number(payload.list_price_ars),
        // M·P2c: el servidor registra la diferencia como ajuste manual en el
        // ledger. Sólo viaja si el usuario lo cambió, con el stock que veía:
        // si entró una venta en el medio responde 409 en vez de pisarla.
        ...(stock
          ? {
              stock_units: Number(payload.stock_units),
              stock_units_esperado: stock.esperado,
              ...(stock.envio ? { costo_envio: stock.envio } : {}),
            }
          : {}),
        // Van juntos (el servidor lo exige). Mandarlos sin cambio no cambia
        // nada; cambiarlos en un producto con historia da 409.
        sale_unit: payload.sale_unit ?? "unit",
        base_units_per_sale_unit: payload.base_units_per_sale_unit ?? 1,
        // null = no configurado (usa default 5 del servidor); 0 = umbral explícito
        low_stock_threshold_units: payload.low_stock_threshold_units == null ? null : Number(payload.low_stock_threshold_units),
        acquired_at: payload.acquired_at ?? null,
      });
      // B·5d: el stock inicial va DESPUÉS del PATCH (que no lleva stock) y por
      // su endpoint: es una apertura al costo, no un ajuste.
      let abierta: Awaited<ReturnType<typeof productsService.registerOpening>> | null = null;
      if (apertura) {
        try {
          abierta = await productsService.registerOpening(
            payload.id,
            {
              stock_units: apertura.stock_units,
              unit_cost_ars: apertura.unit_cost_ars,
              fecha: fechaDeStockInicial(apertura.fecha),
              ...(apertura.envio ? { costo_envio: apertura.envio } : {}),
            },
            nuevaClaveIdempotencia(),
          );
        } catch (causa) {
          throw new AperturaFallida(causa);
        }
      }
      return { actualizado, abierta };
    },
    onSuccess: async ({ abierta }) => {
      setEditing(null);
      setErrorApertura(null);
      const costeadas = abierta?.ventas_sin_costo_desde_la_fecha ?? 0;
      toast(
        abierta
          ? `Producto actualizado y stock inicial cargado.${
              costeadas > 0 ? ` ${costeadas} venta(s) quedaron con costo.` : ""
            }`
          : "Producto actualizado.",
        "success",
      );
      await queryClient.invalidateQueries({ queryKey: ["products-list"] });
    },
    onError: (e) => {
      if (e instanceof AperturaFallida) {
        // El resto ya se guardó: la lista se refresca igual.
        void queryClient.invalidateQueries({ queryKey: ["products-list"] });
        if (esPinCancelado(e.causa)) {
          toast("Se guardaron los demás cambios; el stock inicial no se cargó.", "error");
          return;
        }
        const info = posErrorInfo(e.causa);
        if (info.code === "OPENING_EXISTS") {
          const caminos = Array.isArray(info.fields.caminos)
            ? info.fields.caminos.filter((c): c is string => typeof c === "string")
            : [];
          setErrorApertura({
            mensaje: info.message ?? "Este producto ya tiene stock inicial cargado.",
            caminos,
          });
          return;
        }
        // El 404 del servidor viene en inglés («Product not found.»).
        const motivo =
          info.status === 404
            ? "el producto ya no existe o es de otro negocio."
            : (info.message ?? "probá de nuevo.");
        toast(`Se guardaron los demás cambios, pero no el stock inicial: ${motivo}`, "error");
        return;
      }
      const code = errorCode(e);
      if (code === "STOCK_CAMBIO") {
        // El stock que se veía quedó viejo: sin refrescar, reabrir el producto
        // mandaría el mismo `stock_units_esperado` y el 409 se repetiría.
        void queryClient.invalidateQueries({ queryKey: ["products-list"] });
      }
      toast(
        code === "SALE_UNIT_LOCKED"
          ? "No se puede cambiar la unidad de un producto con stock, ventas o movimientos."
          : code === "STOCK_CAMBIO"
            ? "El stock cambió mientras editabas (entró una venta). Cerrá y volvé a abrir el producto."
            : code === "COSTO_REQUERIDO_CON_STOCK"
              ? COSTO_REQUERIDO_CON_STOCK
              : "No se pudo actualizar el producto.",
        "error",
      );
    },
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
        canCreateCategory={user?.role === "OWNER" || user?.role === "ADMIN"}
        saving={updateMutation.isPending}
        errorApertura={errorApertura}
        onClose={() => {
          setEditing(null);
          setErrorApertura(null);
        }}
        onSave={(product, stock, apertura) => {
          setErrorApertura(null);
          updateMutation.mutate({ product, stock, apertura });
        }}
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

/** M·P2c: un cambio de stock editado a mano. `esperado` = lo que se veía. */
type CambioDeStock = { esperado: number; envio?: TratamientoEnvio };

/** B·5d: la suba es el stock inicial (lo compraron el dueño o los socios).
 *  `stock_units` = la diferencia, en unidades base; `fecha` = "YYYY-MM-DD". */
type AperturaPedida = {
  stock_units: number;
  unit_cost_ars: number;
  fecha: string;
  envio?: TratamientoEnvio;
};

type ErrorDeApertura = { mensaje: string; caminos: string[] };

/** El PATCH salió bien y el stock inicial no: el error lleva la causa. */
class AperturaFallida extends Error {
  constructor(readonly causa: unknown) {
    super("apertura");
  }
}

// Valor centinela del <select> de categoría: abre el alta inline (B·3b).
const NUEVA_CATEGORIA = "__nueva_categoria__";

function ProductEditModal({
  product,
  categories,
  canCreateCategory,
  saving,
  errorApertura,
  onClose,
  onSave,
}: {
  product: ProductResponse | null;
  categories: ProductCategoryOption[];
  /** POST /products/custom-categories es OWNER/ADMIN: el resto no ve la opción. */
  canCreateCategory: boolean;
  saving: boolean;
  errorApertura: ErrorDeApertura | null;
  onClose: () => void;
  /** stock=null: el stock no se tocó y NO va en el PATCH. apertura: la suba es
   *  el stock inicial (va después, por `POST /opening`, con stock=null). */
  onSave: (
    product: ProductResponse,
    stock: CambioDeStock | null,
    apertura: AperturaPedida | null,
  ) => void;
}) {
  const queryClient = useQueryClient();
  const [form, setForm] = useState<ProductResponse | null>(product);
  // B·3b: alta de categoría desde el editor. Mismo endpoint que la carga
  // manual; si el label coincide con una canónica, el servidor devuelve ESA.
  const [creandoCategoria, setCreandoCategoria] = useState(false);
  const [nuevaCategoria, setNuevaCategoria] = useState("");
  const [errorCategoria, setErrorCategoria] = useState<string | null>(null);
  // La recién creada, hasta que el catálogo refetcheado la traiga.
  const [creada, setCreada] = useState<ProductCategoryOption | null>(null);
  // El pedido de alta que el editor abierto todavía espera, o null. Cancelar
  // el alta, elegir otra categoría, cerrar o reabrir lo descartan: la
  // respuesta de un pedido descartado no toca el formulario (el modal queda
  // montado entre aperturas, y la elección explícita del usuario no se pisa).
  // Se compara por identidad del pedido, no por producto: con dos altas en
  // vuelo, sólo la última pedida en este editor se aplica.
  const altaEsperada = useRef<{ label: string; productId: string } | null>(null);
  const crearCategoria = useMutation({
    mutationFn: ({ label }: { label: string; productId: string }) =>
      productsService.createCategory(label),
    onSuccess: async (opcion, pedido) => {
      setCreada(opcion);
      if (altaEsperada.current === pedido) {
        altaEsperada.current = null;
        setForm((prev) => (prev && prev.id === pedido.productId ? { ...prev, category: opcion.code } : prev));
        setCreandoCategoria(false);
        setNuevaCategoria("");
      }
      await queryClient.invalidateQueries({ queryKey: ["product-categories"] });
    },
    onError: (e, pedido) => {
      if (altaEsperada.current !== pedido) return;
      altaEsperada.current = null;
      setErrorCategoria(posErrorInfo(e).message ?? "No se pudo crear la categoría.");
    },
  });
  const opciones =
    creada && !categories.some((c) => c.code === creada.code)
      ? [...categories, creada]
      : categories;
  // Stock y umbral se escriben en la unidad de VENTA (kg, L) y se guardan en
  // unidades base: el texto vive aparte para poder tipear "0,750".
  const [stockText, setStockText] = useState("");
  const [umbralText, setUmbralText] = useState("");
  // M·P2c: por qué sube el stock. Una compra no se carga acá: va por
  // «Registrar compra», que deja el gasto y el proveedor.
  // B·5d: «apertura» = es el stock inicial; sólo si el producto no tiene uno.
  const [motivoSuba, setMotivoSuba] = useState<"" | "correccion" | "compra" | "apertura">("");
  const [envio, setEnvio] = useState<TratamientoEnvio | "">("");
  const [fechaApertura, setFechaApertura] = useState("");
  useEffect(() => {
    altaEsperada.current = null;
    setMotivoSuba("");
    setFechaApertura("");
    setEnvio("");
    setForm(product);
    setCreandoCategoria(false);
    setNuevaCategoria("");
    setErrorCategoria(null);
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
  // Tras un OPENING_EXISTS el producto ya tiene stock inicial: la opción se
  // retira (la lista se refresca aparte) y queda el aviso con los caminos.
  useEffect(() => {
    if (errorApertura) setMotivoSuba((m) => (m === "apertura" ? "" : m));
  }, [errorApertura]);
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
  const subeStock = !!product && stockBase !== null && stockBase > product.stock_units;
  const sinCosto = form.unit_cost_ars == null || Number(form.unit_cost_ars) <= 0;
  const ofreceApertura = product?.needs_initial_stock === true && !errorApertura;
  const fechaAperturaError =
    fechaApertura === ""
      ? "Indicá desde qué fecha tenés este stock."
      : fechaApertura > hoyLocal()
        ? "La fecha no puede ser futura."
        : null;
  // La suba se guarda como corrección o como stock inicial, siempre con precio
  // de compra (M·P2c); el stock inicial, además, con su fecha (B·5d).
  const subaBloqueada =
    subeStock &&
    (sinCosto ||
      !(
        motivoSuba === "correccion" ||
        (motivoSuba === "apertura" && ofreceApertura && fechaAperturaError === null)
      ));
  // Con el alta de categoría abierta (escribiendo o en vuelo), Guardar mandaría
  // la categoría anterior y descartaría lo pedido: primero se crea o se
  // cancela. Cancelada, la respuesta ya no se aplica, así que no hay que esperarla.
  const altaEnVueloAqui =
    crearCategoria.isPending && crearCategoria.variables?.productId === form.id;
  const categoriaAMedias = creandoCategoria;
  const descartarAlta = () => {
    altaEsperada.current = null;
    setCreandoCategoria(false);
  };
  const crear = () => {
    if (!form || !nuevaCategoria.trim() || altaEnVueloAqui) return;
    setErrorCategoria(null);
    const pedido = { label: nuevaCategoria.trim(), productId: form.id };
    altaEsperada.current = pedido;
    crearCategoria.mutate(pedido);
  };
  return (
    <Modal isOpen={!!product} onClose={onClose} title="Editar producto" size="lg">
      <form
        className="grid gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (invalido || categoriaAMedias || subaBloqueada || !product) return;
          if (subeStock && motivoSuba === "apertura" && stockBase !== null) {
            onSave(
              { ...form, stock_units: product.stock_units, low_stock_threshold_units: umbralBase },
              null,
              {
                stock_units: stockBase - product.stock_units,
                unit_cost_ars: Number(form.unit_cost_ars),
                fecha: fechaApertura,
                envio: envio || undefined,
              },
            );
            return;
          }
          onSave(
            { ...form, stock_units: stockBase ?? 0, low_stock_threshold_units: umbralBase },
            stockBase !== product.stock_units
              ? { esperado: product.stock_units, envio: subeStock && envio ? envio : undefined }
              : null,
            null,
          );
        }}
      >
        <label className="grid gap-1 text-sm text-vektor-body">Producto<input className="rounded border border-vk-border-w px-3 py-2" value={form.name} onChange={(e) => set("name", e.target.value)} /></label>
        <div className="grid grid-cols-2 gap-3">
          <label className="grid gap-1 text-sm text-vektor-body">SKU<input className="rounded border border-vk-border-w px-3 py-2" value={form.sku ?? ""} onChange={(e) => set("sku", e.target.value || null)} /></label>
          <label className="grid gap-1 text-sm text-vektor-body">Categoría
            <select
              className="rounded border border-vk-border-w px-3 py-2"
              value={creandoCategoria ? NUEVA_CATEGORIA : form.category ?? ""}
              onChange={(e) => {
                if (e.target.value === NUEVA_CATEGORIA) {
                  setErrorCategoria(null);
                  setCreandoCategoria(true);
                  return;
                }
                descartarAlta();
                set("category", e.target.value || null);
              }}
            >
              <option value="">Sin categoría</option>
              {opciones.map((c) => (
                <option key={c.code} value={c.code}>
                  {c.label}
                </option>
              ))}
              {/* Valor actual fuera del catálogo (legacy/raw): conservarlo como opción */}
              {form.category && !opciones.some((c) => c.code === form.category) ? (
                <option value={form.category}>{form.category}</option>
              ) : null}
              {canCreateCategory ? (
                <option value={NUEVA_CATEGORIA}>+ Nueva categoría</option>
              ) : null}
            </select>
          </label>
        </div>
        {creandoCategoria ? (
          <div className="grid gap-1">
            <div className="flex items-end gap-2">
              <label className="grid flex-1 gap-1 text-sm text-vektor-body">
                Nombre de la nueva categoría
                <input
                  className="rounded border border-vk-border-w px-3 py-2"
                  maxLength={100}
                  value={nuevaCategoria}
                  onChange={(e) => setNuevaCategoria(e.target.value)}
                  onKeyDown={(e) => {
                    // Enter crea la categoría; no guarda el producto a medias.
                    if (e.key !== "Enter") return;
                    e.preventDefault();
                    crear();
                  }}
                />
              </label>
              <button
                type="button"
                disabled={!nuevaCategoria.trim() || altaEnVueloAqui}
                onClick={crear}
                className="rounded bg-vk-blue px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
              >
                {altaEnVueloAqui ? "Creando..." : "Crear categoría"}
              </button>
              <button
                type="button"
                onClick={() => {
                  descartarAlta();
                  setErrorCategoria(null);
                }}
                aria-label="Cancelar nueva categoría"
                className="rounded border border-vk-border-w px-3 py-2 text-sm"
              >
                Cancelar
              </button>
            </div>
            {errorCategoria ? (
              <p role="alert" className="text-xs text-vk-danger">{errorCategoria}</p>
            ) : null}
          </div>
        ) : null}
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
        {subeStock ? (
          <fieldset className="grid gap-2 rounded border border-vk-border-w p-3 text-sm text-vektor-body">
            <legend className="px-1 text-xs font-medium">¿Por qué sube el stock?</legend>
            <label className="flex items-center gap-2">
              <input type="radio" name="motivo-suba" checked={motivoSuba === "correccion"} onChange={() => setMotivoSuba("correccion")} />
              Corrección de conteo: la mercadería ya estaba
            </label>
            <label className="flex items-center gap-2">
              <input type="radio" name="motivo-suba" checked={motivoSuba === "compra"} onChange={() => setMotivoSuba("compra")} />
              La compré ahora
            </label>
            {ofreceApertura ? (
              <label className="flex items-center gap-2">
                <input type="radio" name="motivo-suba" checked={motivoSuba === "apertura"} onChange={() => setMotivoSuba("apertura")} />
                Es mi stock inicial (lo compraron el dueño o los socios)
              </label>
            ) : null}
            {motivoSuba === "compra" ? (
              <p role="alert" className="text-xs text-vk-danger">
                Una compra se registra en «Cargar» → Registrar compra: así quedan el gasto, la caja y el
                proveedor. Volvé el stock a {enUnidadDeVenta(product?.stock_units ?? 0, factor)} para guardar el resto.
              </p>
            ) : null}
            {(motivoSuba === "correccion" || motivoSuba === "apertura") && sinCosto ? (
              <p role="alert" className="text-xs text-vk-danger">{COSTO_REQUERIDO_CON_STOCK}</p>
            ) : null}
            {motivoSuba === "apertura" ? (
              <>
                <p className="text-xs text-vk-text-secondary">
                  {QUE_ES_STOCK_INICIAL} Se usa el Costo de arriba.
                </p>
                <label className="grid gap-1 text-xs font-medium">
                  ¿Desde qué fecha tenés este stock?
                  <input
                    type="date"
                    aria-label="Fecha del stock inicial"
                    max={hoyLocal()}
                    className="rounded border border-vk-border-w px-3 py-2 text-sm font-normal"
                    value={fechaApertura}
                    onChange={(e) => setFechaApertura(e.target.value)}
                  />
                </label>
                {fechaApertura !== "" && fechaAperturaError ? (
                  <p className="text-xs text-vk-danger">{fechaAperturaError}</p>
                ) : null}
              </>
            ) : null}
            {motivoSuba === "correccion" || motivoSuba === "apertura" ? (
              <EnvioSelect value={envio} onChange={setEnvio} />
            ) : null}
            {errorApertura ? (
              <div role="alert" className="grid gap-1 rounded border border-vk-danger/30 bg-vk-danger-bg p-2 text-xs text-vk-danger">
                <p className="font-medium">{errorApertura.mensaje} Los demás cambios se guardaron.</p>
                {errorApertura.caminos.length > 0 ? (
                  <ul className="list-disc pl-4">
                    {errorApertura.caminos.map((c) => (
                      <li key={c}>{c}</li>
                    ))}
                  </ul>
                ) : null}
              </div>
            ) : null}
          </fieldset>
        ) : null}
        <label className="grid gap-1 text-sm text-vektor-body">Descripción<input className="rounded border border-vk-border-w px-3 py-2" value={form.description ?? ""} onChange={(e) => set("description", e.target.value || null)} /></label>
        <label className="grid gap-1 text-sm text-vektor-body">Fecha de alta<input className="rounded border border-vk-border-w px-3 py-2" type="datetime-local" value={form.acquired_at ? toDatetimeLocal(form.acquired_at) : ""} onChange={(e) => set("acquired_at", e.target.value || null)} /></label>
        {categoriaAMedias ? (
          <p className="text-xs text-vektor-muted">Creá o cancelá la categoría nueva antes de guardar.</p>
        ) : null}
        <div className="flex justify-end gap-2 pt-2">
          <button type="button" onClick={onClose} className="rounded border border-vk-border-w px-4 py-2 text-sm">Cancelar</button>
          <button type="submit" disabled={saving || invalido || categoriaAMedias || subaBloqueada} className="rounded bg-vk-blue px-4 py-2 text-sm font-medium text-white disabled:opacity-50">{saving ? "Guardando..." : "Guardar"}</button>
        </div>
      </form>
    </Modal>
  );
}
