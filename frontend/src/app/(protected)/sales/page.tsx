"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil, Trash2 } from "lucide-react";
import { PageWrapper } from "@/components/layout/PageWrapper";
import { ManualEntryLauncher } from "@/features/ingestion/ManualEntryLauncher";
import { StatCard } from "@/components/ui/StatCard";
import { SmartTable } from "@/components/ui/SmartTable";
import { EmptyState } from "@/components/ui/EmptyState";
import { Modal } from "@/components/ui/Modal";
import { salesService, type SaleEntryResponse } from "@/services/sales.service";
import { productsService, type ProductResponse } from "@/services/products.service";
import { customersService, type CustomerResponse } from "@/services/customers.service";
import { fieldDefinitionsService } from "@/services/fieldDefinitions.service";
import { buildEditableCustomFieldColumns } from "@/lib/customFieldsEditable";
import { AddColumnButton } from "@/features/customFields/AddColumnButton";
import { useSaveCustomField } from "@/features/customFields/useSaveCustomField";
import { formatDateTime, toDatetimeLocal } from "@/lib/datetime";
import { useToastStore } from "@/stores/toastStore";
import { PeriodFilter } from "@/components/ui/PeriodFilter";
import { CashCloseButton } from "@/features/cash/CashCloseButton";
import {
  type PeriodValue,
  resolvePeriod,
  resolvePreviousPeriod,
  previousPeriodShortLabel,
} from "@/lib/period";
import { PAYMENT_LABELS } from "@/lib/payment";

// Fiado = cuenta corriente: el backend exige un cliente real (no el centinela
// "Local"). Espejamos esa regla client-side para feedback inmediato.
const FIADO_PAYMENT_METHOD = "account";
// Etiqueta del centinela: las ventas sin cliente registrado se agrupan en "Local".
const LOCAL_CUSTOMER_LABEL = "Local";

function formatARS(value: number): string {
  return new Intl.NumberFormat("es-AR", {
    style: "currency",
    currency: "ARS",
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  }).format(value);
}


/** Categoría de la venta derivada del producto vinculado: código canónico →
 * label del catálogo; OTHER con nombre custom → ese nombre; sin producto o
 * sin categoría → "—". */
function saleCategoryDisplay(
  product: ProductResponse | undefined,
  catalogLabels: Record<string, string>,
): string {
  const cat = (product?.category ?? "").trim();
  if (!cat) return "—";
  if (cat === "OTHER") {
    const label = product?.custom_fields?.category_label;
    if (typeof label === "string" && label.trim()) return label.trim();
  }
  return catalogLabels[cat] ?? cat;
}

function buildColumns(
  productById: Map<string, ProductResponse>,
  catalogLabels: Record<string, string>,
  customerById: Map<string, CustomerResponse>,
) {
  // Resuelve el nombre del cliente de una venta. El centinela "Local" entra al map
  // (include_sentinel), así que sus ventas resuelven a "Local" por nombre. Venta sin
  // cliente → "—"; id que no resuelve (cliente borrado) → "Cliente no identificado"
  // (NO asumimos "Local").
  const customerDisplay = (id: string | null): string => {
    if (!id) return "—";
    return customerById.get(id)?.name ?? "Cliente no identificado";
  };
  return [
  {
    key: "transaction_date",
    header: "Fecha y hora",
    hideable: true,
    render: (v: unknown) => formatDateTime(v),
    csvValue: (v: unknown) => formatDateTime(v),
  },
  {
    key: "notes",
    header: "Concepto",
    hideable: true,
    render: (v: unknown) => String(v ?? "").trim() || "—",
    csvValue: (v: unknown) => String(v ?? "").trim(),
  },
  {
    key: "product_id",
    header: "Producto",
    hideable: true,
    render: (v: unknown) => {
      const id = String(v ?? "");
      return id ? productById.get(id)?.name ?? "Producto no encontrado" : "—";
    },
    csvValue: (v: unknown) => {
      const id = String(v ?? "");
      return id ? productById.get(id)?.name ?? "" : "";
    },
  },
  {
    key: "_category",
    header: "Categoría",
    hideable: true,
    render: (_: unknown, row: SaleEntryResponse) =>
      saleCategoryDisplay(
        row.product_id ? productById.get(row.product_id) : undefined,
        catalogLabels,
      ),
    csvValue: (_: unknown, row: SaleEntryResponse) =>
      saleCategoryDisplay(
        row.product_id ? productById.get(row.product_id) : undefined,
        catalogLabels,
      ),
  },
  {
    key: "customer_id",
    header: "Cliente",
    hideable: true,
    render: (v: unknown) => customerDisplay(v ? String(v) : null),
    csvValue: (v: unknown) => customerDisplay(v ? String(v) : null),
  },
  {
    key: "payment_method",
    header: "Medio de pago",
    hideable: true,
    render: (v: unknown) => PAYMENT_LABELS[String(v)] ?? String(v),
    csvValue: (v: unknown) => PAYMENT_LABELS[String(v)] ?? String(v),
  },
  {
    key: "quantity",
    header: "Cantidad",
    hideable: true,
    render: (v: unknown) => (v != null && Number(v) > 0 ? String(Number(v)) : "—"),
    csvValue: (v: unknown) => String(v ?? ""),
  },
  {
    key: "amount",
    header: "Monto",
    hideable: true,
    render: (v: unknown) => (
      <span className="font-medium text-vk-text-primary">{formatARS(Number(v))}</span>
    ),
    csvValue: (v: unknown) => String(Number(v)),
  },
  ];
}

export default function SalesPage() {
  const [period, setPeriod] = useState<PeriodValue>({
    kind: "preset",
    preset: "this_month",
  });
  const [editing, setEditing] = useState<SaleEntryResponse | null>(null);
  const queryClient = useQueryClient();
  const toast = useToastStore((s) => s.add);
  const { from, to } = resolvePeriod(period);
  const prevDates = resolvePreviousPeriod(period);

  const { data: dateRange = null } = useQuery({
    queryKey: ["sales-date-range"],
    queryFn: () => salesService.getDateRange(),
    staleTime: 10 * 60 * 1000,
  });

  const { data: entries = [], isLoading, isError } = useQuery({
    queryKey: ["sales-entries", from, to],
    queryFn: () => salesService.getAllEntries({ from_date: from, to_date: to }),
    staleTime: 60 * 1000,
  });

  const { data: products = [] } = useQuery({
    queryKey: ["products-list"],
    queryFn: () => productsService.getAllProducts({ is_active: true }),
    staleTime: 2 * 60 * 1000,
  });

  const { data: customers = [] } = useQuery({
    queryKey: ["customers-list"],
    // include_sentinel: el centinela "Local" entra al map para resolver el nombre
    // de las ventas sin cliente en la columna Cliente. El picker del modal lo filtra.
    queryFn: () => customersService.getAllCustomers({ include_sentinel: true }),
    staleTime: 2 * 60 * 1000,
  });

  // Catálogo de categorías del vertical: labels para la columna Categoría
  // (derivada del producto vinculado a cada venta).
  const { data: categories = [] } = useQuery({
    queryKey: ["product-categories"],
    queryFn: () => productsService.getCategories(),
    staleTime: 30 * 60 * 1000,
  });
  const catalogLabels = Object.fromEntries(categories.map((c) => [c.code, c.label]));

  const { data: fieldDefs = [] } = useQuery({
    queryKey: ["field-definitions", "sale"],
    queryFn: () => fieldDefinitionsService.getAll("sale"),
    staleTime: 5 * 60 * 1000,
  });

  const { data: prevEntries = [] } = useQuery({
    queryKey: ["sales-entries-prev", prevDates.from, prevDates.to],
    queryFn: () =>
      salesService.getAllEntries({
        from_date: prevDates.from,
        to_date: prevDates.to,
      }),
    staleTime: 5 * 60 * 1000,
  });

  const updateMutation = useMutation({
    mutationFn: (payload: SaleEntryResponse) =>
      salesService.updateSale(payload.id, {
        amount: Number(payload.amount),
        quantity: Number(payload.quantity),
        transaction_date: payload.transaction_date,
        payment_method: payload.payment_method,
        notes: payload.notes,
        product_id: payload.product_id,
        // null → el backend rutea al centinela "Local". El fiado sin cliente real
        // lo rechaza el backend (y lo pre-validamos en el modal).
        customer_id: payload.customer_id,
      }),
    onSuccess: async () => {
      setEditing(null);
      toast("Venta actualizada.", "success");
      await queryClient.invalidateQueries({ queryKey: ["sales-entries"] });
      await queryClient.invalidateQueries({ queryKey: ["sales-all"] });
    },
    onError: () => toast("No se pudo actualizar la venta.", "error"),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => salesService.deleteSale(id),
    onSuccess: async () => {
      toast("Venta anulada.", "success");
      await queryClient.invalidateQueries({ queryKey: ["sales-entries"] });
      await queryClient.invalidateQueries({ queryKey: ["sales-all"] });
    },
    onError: () => toast("No se pudo anular la venta.", "error"),
  });

  const totalActual = entries.reduce((s, e) => s + Number(e.amount), 0);
  const totalPrev = prevEntries.reduce((s, e) => s + Number(e.amount), 0);
  const ticketPromedio = entries.length > 0 ? totalActual / entries.length : 0;

  let variacionTrend: "up" | "down" | "neutral" = "neutral";
  let variacionLabel: string | undefined;
  if (totalPrev > 0) {
    const pct = ((totalActual - totalPrev) / totalPrev) * 100;
    variacionTrend = pct > 0 ? "up" : pct < 0 ? "down" : "neutral";
    variacionLabel = `${pct > 0 ? "+" : ""}${pct.toFixed(1)}% ${previousPeriodShortLabel(period)}`;
  }

  const sorted = [...entries].sort(
    (a, b) =>
      new Date(b.transaction_date).getTime() - new Date(a.transaction_date).getTime(),
  );
  const productById = new Map(products.map((product) => [product.id, product]));
  // customerById incluye el centinela "Local" (para resolver el nombre en la
  // columna Cliente); realCustomers lo excluye (para el picker del modal de edición).
  const customerById = new Map(customers.map((c) => [c.id, c]));
  const realCustomers = customers.filter((c) => !c.is_sentinel);

  const saveCustomField = useSaveCustomField({
    listKey: ["sales-entries"],
    update: (id, custom_fields) => salesService.updateSale(id, { custom_fields }),
  });

  const columns = [
    ...buildColumns(productById, catalogLabels, customerById),
    ...buildEditableCustomFieldColumns<SaleEntryResponse>(fieldDefs, saveCustomField),
  ];

  return (
    <PageWrapper title="Ventas" actions={<ManualEntryLauncher />}>
      {/* Period filter + cierre de caja */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <PeriodFilter value={period} onChange={setPeriod} availableRange={dateRange} />
        <CashCloseButton />
      </div>

      {/* KPI skeleton */}
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
          <StatCard
            label="Total del período"
            value={formatARS(totalActual)}
            trend={variacionTrend}
            trendValue={variacionLabel}
          />
          <StatCard
            label="Ticket promedio"
            value={entries.length > 0 ? formatARS(ticketPromedio) : "—"}
          />
          <StatCard label="Transacciones" value={entries.length} />
          <StatCard
            label="Mes anterior"
            value={totalPrev > 0 ? formatARS(totalPrev) : "—"}
            sublabel="total"
          />
        </div>
      )}

      {/* Table */}
      {isError ? (
        <p className="rounded-lg border border-vk-danger/20 bg-vk-danger-bg px-4 py-3 text-sm text-vk-danger">
          Error al cargar las ventas. Recargá la página.
        </p>
      ) : !isLoading && entries.length === 0 ? (
        <EmptyState
          title="Sin ventas en este período"
          description="Registrá tus ventas usando el chat."
          action={{ label: "Ir al chat", href: "/chat" }}
        />
      ) : (
        <SmartTable
          columns={columns}
          data={sorted}
          exportFilename="vektor-ventas"
          toolbarActions={<AddColumnButton entityType="sale" entityLabel="Ventas" />}
          renderActions={(row) => (
            <div className="flex items-center gap-1">
              <button
                type="button"
                title="Editar"
                aria-label="Editar venta"
                onClick={() => setEditing(row)}
                className="inline-flex h-8 w-8 items-center justify-center rounded border border-vk-border-w text-vk-text-secondary transition-colors hover:bg-vk-bg-light hover:text-vk-text-primary"
              >
                <Pencil className="h-4 w-4" />
              </button>
              <button
                type="button"
                title="Anular"
                aria-label="Anular venta"
                disabled={deleteMutation.isPending}
                onClick={() => {
                  if (confirm("¿Anular esta venta?")) {
                    deleteMutation.mutate(row.id);
                  }
                }}
                className="inline-flex h-8 w-8 items-center justify-center rounded border border-vk-danger/30 text-vk-danger transition-colors hover:bg-vk-danger-bg disabled:opacity-50"
              >
                <Trash2 className="h-4 w-4" />
              </button>
            </div>
          )}
        />
      )}
      <SaleEditModal
        sale={editing}
        saving={updateMutation.isPending}
        onClose={() => setEditing(null)}
        onSave={(sale) => updateMutation.mutate(sale)}
        products={products}
        customers={realCustomers}
      />
    </PageWrapper>
  );
}

function SaleEditModal({
  sale,
  saving,
  onClose,
  onSave,
  products,
  customers,
}: {
  sale: SaleEntryResponse | null;
  saving: boolean;
  onClose: () => void;
  onSave: (sale: SaleEntryResponse) => void;
  products: ProductResponse[];
  customers: CustomerResponse[];
}) {
  const [form, setForm] = useState<SaleEntryResponse | null>(sale);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setForm(sale);
    setError(null);
  }, [sale]);

  if (!form) {
    return null;
  }

  const set = (key: keyof SaleEntryResponse, value: string | number | null) => {
    setForm((prev) => (prev ? { ...prev, [key]: value } : prev));
    if (error) setError(null);
  };

  // El select solo lista clientes reales; el centinela "Local" se representa con "".
  // Si el customer_id de la venta no está en la lista (= centinela), lo tratamos
  // como "Local" para que el select no quede en un valor fantasma.
  const customerExists = !!form.customer_id && customers.some((c) => c.id === form.customer_id);
  const selectValue = customerExists ? (form.customer_id as string) : "";
  const isFiado = form.payment_method === FIADO_PAYMENT_METHOD;

  const setProduct = (productId: string) => {
    const product = products.find((p) => p.id === productId);
    setForm((prev) => {
      if (!prev) return prev;
      const next: SaleEntryResponse = { ...prev, product_id: productId || null };
      if (product && Number(prev.quantity) > 0) {
        next.amount = Number(product.sale_price_ars) * Number(prev.quantity);
      }
      return next;
    });
  };

  return (
    <Modal isOpen={!!sale} onClose={onClose} title="Editar venta" size="lg">
      <form
        className="grid gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (form.payment_method === FIADO_PAYMENT_METHOD && !customerExists) {
            setError("El fiado (cuenta corriente) requiere un cliente registrado, no puede ser 'Local'.");
            return;
          }
          onSave(form);
        }}
      >
        {error && (
          <p className="rounded-lg border border-vk-danger/30 bg-vk-danger-bg px-3 py-2 text-sm text-vk-danger">
            {error}
          </p>
        )}
        <div className="grid grid-cols-2 gap-3">
          <label className="grid gap-1 text-sm text-vektor-body">
            Fecha y hora
            <input className="rounded border border-vk-border-w px-3 py-2" type="datetime-local" value={toDatetimeLocal(form.transaction_date)} onChange={(e) => set("transaction_date", e.target.value)} />
          </label>
          <label className="grid gap-1 text-sm text-vektor-body">
            Medio de pago
            <select className="rounded border border-vk-border-w px-3 py-2" value={form.payment_method} onChange={(e) => set("payment_method", e.target.value)}>
              {Object.entries(PAYMENT_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>
        </div>
        <label className="grid gap-1 text-sm text-vektor-body">
          Concepto
          <input className="rounded border border-vk-border-w px-3 py-2" value={form.notes ?? ""} onChange={(e) => set("notes", e.target.value)} />
        </label>
        <label className="grid gap-1 text-sm text-vektor-body">
          Producto
          <select
            className="rounded border border-vk-border-w px-3 py-2"
            value={form.product_id ?? ""}
            onChange={(e) => setProduct(e.target.value)}
          >
            <option value="">Sin producto asociado</option>
            {products.map((product) => (
              <option key={product.id} value={product.id}>
                {product.name} · {formatARS(Number(product.sale_price_ars))}
              </option>
            ))}
          </select>
        </label>
        <label className="grid gap-1 text-sm text-vektor-body">
          Cliente
          <select
            className="rounded border border-vk-border-w px-3 py-2"
            value={selectValue}
            onChange={(e) => set("customer_id", e.target.value || null)}
          >
            <option value="">{LOCAL_CUSTOMER_LABEL} (mostrador, sin cliente registrado)</option>
            {customers.map((customer) => (
              <option key={customer.id} value={customer.id}>
                {customer.name}
                {customer.last_name?.trim() ? ` ${customer.last_name.trim()}` : ""}
              </option>
            ))}
          </select>
          {isFiado && !customerExists ? (
            <span className="text-xs text-vk-danger">
              El fiado requiere un cliente registrado.
            </span>
          ) : null}
        </label>
        <div className="grid grid-cols-2 gap-3">
          <label className="grid gap-1 text-sm text-vektor-body">
            Cantidad
            <input className="rounded border border-vk-border-w px-3 py-2" type="number" min={1} value={form.quantity} onChange={(e) => set("quantity", Number(e.target.value))} />
          </label>
          <label className="grid gap-1 text-sm text-vektor-body">
            Monto
            <input className="rounded border border-vk-border-w px-3 py-2" type="number" min={0} step="0.01" value={form.amount} onChange={(e) => set("amount", Number(e.target.value))} />
          </label>
        </div>
        <div className="flex justify-end gap-2 pt-2">
          <button type="button" onClick={onClose} className="rounded border border-vk-border-w px-4 py-2 text-sm">Cancelar</button>
          <button type="submit" disabled={saving} className="rounded bg-vk-blue px-4 py-2 text-sm font-medium text-white disabled:opacity-50">
            {saving ? "Guardando..." : "Guardar"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
