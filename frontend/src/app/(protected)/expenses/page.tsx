"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil, Trash2 } from "lucide-react";
import { PageWrapper } from "@/components/layout/PageWrapper";
import { StatCard } from "@/components/ui/StatCard";
import { SmartTable } from "@/components/ui/SmartTable";
import { Badge } from "@/components/ui/Badge";
import { EmptyState } from "@/components/ui/EmptyState";
import { Modal } from "@/components/ui/Modal";
import { expensesService, type ExpenseEntryResponse } from "@/services/expenses.service";
import { fieldDefinitionsService } from "@/services/fieldDefinitions.service";
import { buildCustomFieldColumns } from "@/lib/customFields";
import { formatDateTime, toDatetimeLocal } from "@/lib/datetime";
import { useToastStore } from "@/stores/toastStore";
import { PeriodFilter } from "@/components/ui/PeriodFilter";
import {
  type PeriodValue,
  resolvePeriod,
  resolvePreviousPeriod,
  previousPeriodShortLabel,
} from "@/lib/period";

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
    header: "Fecha y hora",
    hideable: true,
    render: (v: unknown) => formatDateTime(v),
    csvValue: (v: unknown) => formatDateTime(v),
  },
  {
    key: "category",
    header: "Categoría",
    hideable: true,
    render: (v: unknown) => {
      const cat = String(v);
      return (
        <Badge variant={CATEGORY_VARIANTS[cat] ?? "default"}>
          {CATEGORY_LABELS[cat] ?? cat}
        </Badge>
      );
    },
    csvValue: (v: unknown) => CATEGORY_LABELS[String(v)] ?? String(v),
  },
  {
    key: "description",
    header: "Descripción",
    hideable: true,
    render: (v: unknown) => String(v ?? "").trim() || "—",
    csvValue: (v: unknown) => String(v ?? "").trim(),
  },
  {
    key: "supplier_name",
    header: "Proveedor",
    hideable: true,
    defaultVisible: false,
    render: (v: unknown) => String(v ?? "").trim() || "—",
    csvValue: (v: unknown) => String(v ?? "").trim(),
  },
  {
    key: "is_recurring",
    header: "Recurrente",
    hideable: true,
    defaultVisible: false,
    render: (v: unknown) => (v ? "Sí" : "No"),
    csvValue: (v: unknown) => (v ? "Sí" : "No"),
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

export default function ExpensesPage() {
  const [period, setPeriod] = useState<PeriodValue>({
    kind: "preset",
    preset: "this_month",
  });
  const [categoryFilter, setCategoryFilter] = useState("all");
  const [editing, setEditing] = useState<ExpenseEntryResponse | null>(null);
  const queryClient = useQueryClient();
  const toast = useToastStore((s) => s.add);

  const { from, to } = resolvePeriod(period);
  const prevDates = resolvePreviousPeriod(period);

  const { data: dateRange = null } = useQuery({
    queryKey: ["expenses-date-range"],
    queryFn: () => expensesService.getDateRange(),
    staleTime: 10 * 60 * 1000,
  });

  const { data: entries = [], isLoading, isError } = useQuery({
    queryKey: ["expenses-entries", from, to],
    queryFn: () => expensesService.getAllEntries({ from_date: from, to_date: to }),
    staleTime: 60 * 1000,
  });

  const { data: fieldDefs = [] } = useQuery({
    queryKey: ["field-definitions", "expense"],
    queryFn: () => fieldDefinitionsService.getAll("expense"),
    staleTime: 5 * 60 * 1000,
  });

  const { data: prevEntries = [] } = useQuery({
    queryKey: ["expenses-entries-prev", prevDates.from, prevDates.to],
    queryFn: () =>
      expensesService.getAllEntries({
        from_date: prevDates.from,
        to_date: prevDates.to,
      }),
    staleTime: 5 * 60 * 1000,
  });

  const updateMutation = useMutation({
    mutationFn: (payload: ExpenseEntryResponse) =>
      expensesService.updateExpense(payload.id, {
        amount: Number(payload.amount),
        category: payload.category,
        expense_date: payload.transaction_date,
        description: payload.description,
        is_recurring: payload.is_recurring,
        payment_method: payload.payment_method,
        supplier_name: payload.supplier_name,
        notes: payload.notes,
      }),
    onSuccess: async () => {
      setEditing(null);
      toast("Gasto actualizado.", "success");
      await queryClient.invalidateQueries({ queryKey: ["expenses-entries"] });
    },
    onError: () => toast("No se pudo actualizar el gasto.", "error"),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => expensesService.deleteExpense(id),
    onSuccess: async () => {
      toast("Gasto anulado.", "success");
      await queryClient.invalidateQueries({ queryKey: ["expenses-entries"] });
    },
    onError: () => toast("No se pudo anular el gasto.", "error"),
  });

  // KPI calculations
  const totalActual = entries.reduce((s, e) => s + Number(e.amount), 0);
  const totalPrev = prevEntries.reduce((s, e) => s + Number(e.amount), 0);

  // Top category
  const categoryTotals = entries.reduce<Record<string, number>>((acc, e) => {
    acc[e.category] = (acc[e.category] ?? 0) + Number(e.amount);
    return acc;
  }, {});
  const topCat = Object.entries(categoryTotals).sort((a, b) => b[1] - a[1])[0];
  const topCatLabel = topCat
    ? `${CATEGORY_LABELS[topCat[0]] ?? topCat[0]}: ${formatARS(topCat[1])}`
    : "—";

  let variacionTrend: "up" | "down" | "neutral" = "neutral";
  let variacionLabel: string | undefined;
  if (totalPrev > 0) {
    const pct = ((totalActual - totalPrev) / totalPrev) * 100;
    // For expenses, going up is "bad" (danger = down trend from a health perspective)
    variacionTrend = pct > 0 ? "down" : pct < 0 ? "up" : "neutral";
    variacionLabel = `${pct > 0 ? "+" : ""}${pct.toFixed(1)}% ${previousPeriodShortLabel(period)}`;
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
      <div className="flex flex-col gap-3">
        <PeriodFilter value={period} onChange={setPeriod} availableRange={dateRange} />
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
        <SmartTable
          columns={[...COLUMNS, ...buildCustomFieldColumns<ExpenseEntryResponse>(fieldDefs)]}
          data={sorted}
          exportFilename="vektor-gastos"
          renderActions={(row) => (
            <div className="flex items-center gap-1">
              <button type="button" title="Editar" aria-label="Editar gasto" onClick={() => setEditing(row)} className="inline-flex h-8 w-8 items-center justify-center rounded border border-vk-border-w text-vk-text-secondary transition-colors hover:bg-vk-bg-light hover:text-vk-text-primary">
                <Pencil className="h-4 w-4" />
              </button>
              <button
                type="button"
                title="Anular"
                aria-label="Anular gasto"
                disabled={deleteMutation.isPending}
                onClick={() => {
                  if (confirm("¿Anular este gasto?")) deleteMutation.mutate(row.id);
                }}
                className="inline-flex h-8 w-8 items-center justify-center rounded border border-vk-danger/30 text-vk-danger transition-colors hover:bg-vk-danger-bg disabled:opacity-50"
              >
                <Trash2 className="h-4 w-4" />
              </button>
            </div>
          )}
        />
      )}
      <ExpenseEditModal
        expense={editing}
        saving={updateMutation.isPending}
        onClose={() => setEditing(null)}
        onSave={(expense) => updateMutation.mutate(expense)}
      />
    </PageWrapper>
  );
}

function ExpenseEditModal({
  expense,
  saving,
  onClose,
  onSave,
}: {
  expense: ExpenseEntryResponse | null;
  saving: boolean;
  onClose: () => void;
  onSave: (expense: ExpenseEntryResponse) => void;
}) {
  const [form, setForm] = useState<ExpenseEntryResponse | null>(expense);
  useEffect(() => {
    setForm(expense);
  }, [expense]);
  if (!form) return null;
  const set = (key: keyof ExpenseEntryResponse, value: string | number | boolean | null) => {
    setForm((prev) => (prev ? { ...prev, [key]: value } : prev));
  };
  return (
    <Modal isOpen={!!expense} onClose={onClose} title="Editar gasto" size="lg">
      <form className="grid gap-4" onSubmit={(event) => { event.preventDefault(); onSave(form); }}>
        <div className="grid grid-cols-2 gap-3">
          <label className="grid gap-1 text-sm text-vk-text-secondary">Fecha y hora<input className="rounded border border-vk-border-w px-3 py-2" type="datetime-local" value={toDatetimeLocal(form.transaction_date)} onChange={(e) => set("transaction_date", e.target.value)} /></label>
          <label className="grid gap-1 text-sm text-vk-text-secondary">Categoría<select className="rounded border border-vk-border-w px-3 py-2" value={form.category} onChange={(e) => set("category", e.target.value)}>{ALL_CATEGORIES.map((cat) => <option key={cat} value={cat}>{CATEGORY_LABELS[cat] ?? cat}</option>)}</select></label>
        </div>
        <label className="grid gap-1 text-sm text-vk-text-secondary">Descripción<input className="rounded border border-vk-border-w px-3 py-2" value={form.description ?? ""} onChange={(e) => set("description", e.target.value)} /></label>
        <div className="grid grid-cols-2 gap-3">
          <label className="grid gap-1 text-sm text-vk-text-secondary">Proveedor<input className="rounded border border-vk-border-w px-3 py-2" value={form.supplier_name ?? ""} onChange={(e) => set("supplier_name", e.target.value || null)} /></label>
          <label className="grid gap-1 text-sm text-vk-text-secondary">Monto<input className="rounded border border-vk-border-w px-3 py-2" type="number" min={0} step="0.01" value={form.amount} onChange={(e) => set("amount", Number(e.target.value))} /></label>
        </div>
        <label className="flex items-center gap-2 text-sm text-vk-text-secondary"><input type="checkbox" checked={form.is_recurring} onChange={(e) => set("is_recurring", e.target.checked)} />Recurrente</label>
        <div className="flex justify-end gap-2 pt-2">
          <button type="button" onClick={onClose} className="rounded border border-vk-border-w px-4 py-2 text-sm">Cancelar</button>
          <button type="submit" disabled={saving} className="rounded bg-vk-blue px-4 py-2 text-sm font-medium text-white disabled:opacity-50">{saving ? "Guardando..." : "Guardar"}</button>
        </div>
      </form>
    </Modal>
  );
}
