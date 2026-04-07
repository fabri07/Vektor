"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { PageWrapper } from "@/components/layout/PageWrapper";
import { StatCard } from "@/components/ui/StatCard";
import { Table } from "@/components/ui/Table";
import { EmptyState } from "@/components/ui/EmptyState";
import { salesService, type SaleEntryResponse } from "@/services/sales.service";

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
    render: (v: unknown) =>
      new Date(String(v) + "T00:00:00").toLocaleDateString("es-AR", {
        day: "2-digit",
        month: "short",
        year: "numeric",
      }),
  },
  {
    key: "notes",
    header: "Concepto",
    render: (v: unknown) => String(v ?? "").trim() || "—",
  },
  {
    key: "payment_method",
    header: "Medio de pago",
    render: (v: unknown) => PAYMENT_LABELS[String(v)] ?? String(v),
  },
  {
    key: "amount",
    header: "Monto",
    render: (v: unknown) => (
      <span className="font-medium text-vk-text-primary">{formatARS(Number(v))}</span>
    ),
  },
];

const PERIOD_OPTIONS: { value: PeriodFilter; label: string }[] = [
  { value: "month", label: "Este mes" },
  { value: "week", label: "Semana actual" },
  { value: "prev_month", label: "Mes anterior" },
];

export default function SalesPage() {
  const [period, setPeriod] = useState<PeriodFilter>("month");
  const { from, to } = getPeriodDates(period);
  const prevDates = getPeriodDates("prev_month");

  const { data: entries = [], isLoading, isError } = useQuery({
    queryKey: ["sales-entries", from, to],
    queryFn: () => salesService.getEntries({ from_date: from, to_date: to, limit: 200 }),
    staleTime: 60 * 1000,
  });

  const { data: prevEntries = [] } = useQuery({
    queryKey: ["sales-entries-prev", prevDates.from, prevDates.to],
    queryFn: () =>
      salesService.getEntries({
        from_date: prevDates.from,
        to_date: prevDates.to,
        limit: 200,
      }),
    staleTime: 5 * 60 * 1000,
    enabled: period === "month",
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
        <Table columns={COLUMNS} data={sorted as Record<string, unknown>[]} />
      )}
    </PageWrapper>
  );
}
