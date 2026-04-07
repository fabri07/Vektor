"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { PageWrapper } from "@/components/layout/PageWrapper";
import { StatCard } from "@/components/ui/StatCard";
import { Table } from "@/components/ui/Table";
import { Badge } from "@/components/ui/Badge";
import { EmptyState } from "@/components/ui/EmptyState";
import { expensesService, type ExpenseEntryResponse } from "@/services/expenses.service";

type PeriodFilter = "month" | "prev_month";

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

const CATEGORY_LABELS: Record<string, string> = {
  RENT: "Alquiler",
  UTILITIES: "Servicios",
  PAYROLL: "Sueldos",
  INVENTORY: "Inventario",
  MARKETING: "Marketing",
  OTHER: "Otros",
};

const CATEGORY_VARIANTS: Record<string, "default" | "info" | "warning" | "danger" | "success"> = {
  RENT: "info",
  UTILITIES: "warning",
  PAYROLL: "danger",
  INVENTORY: "success",
  MARKETING: "info",
  OTHER: "default",
};

const ALL_CATEGORIES = Object.keys(CATEGORY_LABELS);

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
    key: "category",
    header: "Categoría",
    render: (v: unknown) => {
      const cat = String(v);
      return (
        <Badge variant={CATEGORY_VARIANTS[cat] ?? "default"}>
          {CATEGORY_LABELS[cat] ?? cat}
        </Badge>
      );
    },
  },
  {
    key: "description",
    header: "Descripción",
    render: (v: unknown) => String(v ?? "").trim() || "—",
  },
  {
    key: "supplier_name",
    header: "Proveedor",
    render: (v: unknown) => String(v ?? "").trim() || "—",
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
  { value: "prev_month", label: "Mes anterior" },
];

export default function ExpensesPage() {
  const [period, setPeriod] = useState<PeriodFilter>("month");
  const [categoryFilter, setCategoryFilter] = useState("all");

  const { from, to } = getPeriodDates(period);
  const prevDates = getPeriodDates("prev_month");

  const { data: entries = [], isLoading, isError } = useQuery({
    queryKey: ["expenses-entries", from, to],
    queryFn: () => expensesService.getAllEntries({ from_date: from, to_date: to }),
    staleTime: 60 * 1000,
  });

  const { data: prevEntries = [] } = useQuery({
    queryKey: ["expenses-entries-prev", prevDates.from, prevDates.to],
    queryFn: () =>
      expensesService.getAllEntries({
        from_date: prevDates.from,
        to_date: prevDates.to,
      }),
    staleTime: 5 * 60 * 1000,
    enabled: period === "month",
  });

  // KPI calculations
  const totalActual = entries.reduce((s, e) => s + e.amount, 0);
  const totalPrev = prevEntries.reduce((s, e) => s + e.amount, 0);

  // Top category
  const categoryTotals = entries.reduce<Record<string, number>>((acc, e) => {
    acc[e.category] = (acc[e.category] ?? 0) + e.amount;
    return acc;
  }, {});
  const topCat = Object.entries(categoryTotals).sort((a, b) => b[1] - a[1])[0];
  const topCatLabel = topCat
    ? `${CATEGORY_LABELS[topCat[0]] ?? topCat[0]}: ${formatARS(topCat[1])}`
    : "—";

  let variacionTrend: "up" | "down" | "neutral" = "neutral";
  let variacionLabel: string | undefined;
  if (period === "month" && totalPrev > 0) {
    const pct = ((totalActual - totalPrev) / totalPrev) * 100;
    // For expenses, going up is "bad" (danger = down trend from a health perspective)
    variacionTrend = pct > 0 ? "down" : pct < 0 ? "up" : "neutral";
    variacionLabel = `${pct > 0 ? "+" : ""}${pct.toFixed(1)}% vs mes ant.`;
  }

  // Apply category filter
  const filtered =
    categoryFilter === "all"
      ? entries
      : entries.filter((e) => e.category === categoryFilter);

  const sorted = [...filtered].sort(
    (a, b) =>
      new Date(b.transaction_date).getTime() - new Date(a.transaction_date).getTime(),
  );

  return (
    <PageWrapper title="Gastos">
      {/* Filters */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2">
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
        <div className="flex items-center gap-2">
          <label className="text-sm text-vk-text-muted">Categoría:</label>
          <select
            value={categoryFilter}
            onChange={(e) => setCategoryFilter(e.target.value)}
            className="rounded-lg border border-vk-border-w bg-vk-surface-w px-3 py-1.5 text-sm text-vk-text-primary focus:outline-none focus:ring-2 focus:ring-vk-blue/20"
          >
            <option value="all">Todas</option>
            {ALL_CATEGORIES.map((cat) => (
              <option key={cat} value={cat}>
                {CATEGORY_LABELS[cat] ?? cat}
              </option>
            ))}
          </select>
        </div>
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
          <StatCard
            label="Total del período"
            value={formatARS(totalActual)}
            trend={variacionTrend}
            trendValue={variacionLabel}
          />
          <StatCard label="Mayor categoría" value={topCatLabel} />
          <StatCard label="N.° de gastos" value={entries.length} />
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
          Error al cargar los gastos. Recargá la página.
        </p>
      ) : !isLoading && filtered.length === 0 ? (
        <EmptyState
          title="Sin gastos en este período"
          description="Registrá tus gastos usando el chat."
          action={{ label: "Ir al chat", href: "/chat" }}
        />
      ) : (
        <Table columns={COLUMNS} data={sorted} />
      )}
    </PageWrapper>
  );
}
