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
import { useToastStore } from "@/stores/toastStore";

type PeriodFilter = "month" | "week" | "prev_month";

function getPeriodDates(filter: PeriodFilter): { from: string; to: string } {
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  const fmt = (d: Date) =>
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

  if (filter === "month") {
    return {
      from: fmt(new Date(now.getFullYear(), now.getMonth(), 1)),
      to: fmt(now),
    };
  }
  if (filter === "week") {
    const day = now.getDay();
    const diff = day === 0 ? -6 : 1 - day;
    const monday = new Date(now);
    monday.setDate(now.getDate() + diff);
    return { from: fmt(monday), to: fmt(now) };
  }
  // prev_month
  return {
    from: fmt(new Date(now.getFullYear(), now.getMonth() - 1, 1)),
    to: fmt(new Date(now.getFullYear(), now.getMonth(), 0)),
  };
}

function formatARS(value: number): string {
  return new Intl.NumberFormat("es-AR", {
    style: "currency",
    currency: "ARS",
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  }).format(value);
}

const PAYMENT_LABELS: Record<string, string> = {
  cash: "Efectivo",
  debit_card: "Débito",
  credit_card: "Crédito",
  transfer: "Transferencia",
  qr: "QR",
  other: "Otro",
};

const COLUMNS = [
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
    defaultVisible: false,
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

const PERIOD_OPTIONS: { value: PeriodFilter; label: string }[] = [
  { value: "month", label: "Este mes" },
  { value: "week", label: "Semana actual" },
  { value: "prev_month", label: "Mes anterior" },
];

export default function SalesPage() {
  const [period, setPeriod] = useState<PeriodFilter>("month");
  const [editing, setEditing] = useState<SaleEntryResponse | null>(null);
  const queryClient = useQueryClient();
  const toast = useToastStore((s) => s.add);
  const { from, to } = getPeriodDates(period);
  const prevDates = getPeriodDates("prev_month");

  const { data: entries = [], isLoading, isError } = useQuery({
    queryKey: ["sales-entries", from, to],
    queryFn: () => salesService.getAllEntries({ from_date: from, to_date: to }),
    staleTime: 60 * 1000,
  });

  const { data: prevEntries = [] } = useQuery({
    queryKey: ["sales-entries-prev", prevDates.from, prevDates.to],
    queryFn: () =>
      salesService.getAllEntries({
        from_date: prevDates.from,
        to_date: prevDates.to,
      }),
    staleTime: 5 * 60 * 1000,
    enabled: period === "month",
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

  const totalActual = entries.reduce((s, e) => s + e.amount, 0);
  const totalPrev = prevEntries.reduce((s, e) => s + e.amount, 0);
  const ticketPromedio = entries.length > 0 ? totalActual / entries.length : 0;

  let variacionTrend: "up" | "down" | "neutral" = "neutral";
  let variacionLabel: string | undefined;
  if (period === "month" && totalPrev > 0) {
    const pct = ((totalActual - totalPrev) / totalPrev) * 100;
    variacionTrend = pct > 0 ? "up" : pct < 0 ? "down" : "neutral";
    variacionLabel = `${pct > 0 ? "+" : ""}${pct.toFixed(1)}% vs mes ant.`;
  }

  const sorted = [...entries].sort(
    (a, b) =>
      new Date(b.transaction_date).getTime() - new Date(a.transaction_date).getTime(),
  );

  return (
    <PageWrapper title="Ventas">
      {/* Period filter */}
      <div className="flex items-center gap-3">
        <label className="text-sm text-vk-text-muted">Período:</label>
        <select
          value={period}
          onChange={(e) => setPeriod(e.target.value as PeriodFilter)}
          className="rounded-lg border border-vk-border-w bg-vk-surface-w px-3 py-1.5 text-sm text-vk-text-primary focus:outline-none focus:ring-2 focus:ring-vk-blue/20"
        >
          {PERIOD_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
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
          columns={COLUMNS}
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
      />
    </PageWrapper>
  );
}

function SaleEditModal({
  sale,
  saving,
  onClose,
  onSave,
}: {
  sale: SaleEntryResponse | null;
  saving: boolean;
  onClose: () => void;
  onSave: (sale: SaleEntryResponse) => void;
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
