"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil, Trash2 } from "lucide-react";
import { PageWrapper } from "@/components/layout/PageWrapper";
import { StatCard } from "@/components/ui/StatCard";
import { SmartTable } from "@/components/ui/SmartTable";
import { EmptyState } from "@/components/ui/EmptyState";
import { Modal } from "@/components/ui/Modal";
import { salesService, type SaleEntryResponse } from "@/services/sales.service";
import { productsService, type ProductResponse } from "@/services/products.service";
import { fieldDefinitionsService } from "@/services/fieldDefinitions.service";
import { buildCustomFieldColumns } from "@/lib/customFields";
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

function formatARS(value: number): string {
  return new Intl.NumberFormat("es-AR", {
    style: "currency",
    currency: "ARS",
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  }).format(value);
}


function buildColumns(productById: Map<string, ProductResponse>) {
  return [
  {
    key: "transaction_date",
    header: "Fecha",
    hideable: true,
    render: (v: unknown) =>
      new Date(String(v) + "T00:00:00").toLocaleDateString("es-AR", {
        day: "2-digit",
        month: "short",
        year: "numeric",
      }),
    csvValue: (v: unknown) => String(v),
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
  const columns = [
    ...buildColumns(productById),
    ...buildCustomFieldColumns<SaleEntryResponse>(fieldDefs),
  ];

  return (
    <PageWrapper title="Ventas">
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
}: {
  sale: SaleEntryResponse | null;
  saving: boolean;
  onClose: () => void;
  onSave: (sale: SaleEntryResponse) => void;
  products: ProductResponse[];
}) {
  const [form, setForm] = useState<SaleEntryResponse | null>(sale);

  useEffect(() => {
    setForm(sale);
  }, [sale]);

  if (!form) {
    return null;
  }

  const set = (key: keyof SaleEntryResponse, value: string | number | null) => {
    setForm((prev) => (prev ? { ...prev, [key]: value } : prev));
  };

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
          onSave(form);
        }}
      >
        <div className="grid grid-cols-2 gap-3">
          <label className="grid gap-1 text-sm text-vk-text-secondary">
            Fecha
            <input className="rounded border border-vk-border-w px-3 py-2" type="date" value={form.transaction_date} onChange={(e) => set("transaction_date", e.target.value)} />
          </label>
          <label className="grid gap-1 text-sm text-vk-text-secondary">
            Medio de pago
            <select className="rounded border border-vk-border-w px-3 py-2" value={form.payment_method} onChange={(e) => set("payment_method", e.target.value)}>
              {Object.entries(PAYMENT_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>
        </div>
        <label className="grid gap-1 text-sm text-vk-text-secondary">
          Concepto
          <input className="rounded border border-vk-border-w px-3 py-2" value={form.notes ?? ""} onChange={(e) => set("notes", e.target.value)} />
        </label>
        <label className="grid gap-1 text-sm text-vk-text-secondary">
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
        <div className="grid grid-cols-2 gap-3">
          <label className="grid gap-1 text-sm text-vk-text-secondary">
            Cantidad
            <input className="rounded border border-vk-border-w px-3 py-2" type="number" min={1} value={form.quantity} onChange={(e) => set("quantity", Number(e.target.value))} />
          </label>
          <label className="grid gap-1 text-sm text-vk-text-secondary">
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
