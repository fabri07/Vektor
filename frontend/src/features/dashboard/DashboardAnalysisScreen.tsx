"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchBusinessBreakdown, fetchCashForecast, fetchCurrentInsight } from "@/services/dashboard.service";
import type { BusinessBreakdownResponse, CashForecastResponse, HealthScoreV2Response } from "@/types/api";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip as RechartsTooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Select } from "@/components/ui/Select";
import { Tooltip } from "@/components/ui/Tooltip";
import type { ExpenseEntryResponse } from "@/services/expenses.service";
import { CATEGORY_LABELS } from "@/lib/expenseCategories";
import type { ProductResponse } from "@/services/products.service";
import type { SaleEntryResponse } from "@/services/sales.service";
import {
  buildComparisonSeries,
  buildDistributionSeries,
  buildLineSeries,
  COMPARE_OPTIONS,
  DISTRIBUTION_OPTIONS,
  formatARS,
  formatARSCompact,
  formatPercent,
  formatSignedPercent,
  LINE_METRIC_OPTIONS,
} from "@/features/dashboard/dashboardData";

interface Props {
  sales: SaleEntryResponse[];
  expenses: ExpenseEntryResponse[];
  products: ProductResponse[];
  scoreHistory?: HealthScoreV2Response[];
  loading?: boolean;
}

const CHART_COLORS = [
  "#3a86ff",
  "#27c7b8",
  "#f1b648",
  "#f06c79",
  "#79aefc",
  "#8be1d8",
];

function InsightBlock() {
  const { data, isLoading } = useQuery({
    queryKey: ["current-insight"],
    queryFn: fetchCurrentInsight,
    staleTime: 5 * 60 * 1000,
    retry: false,
  });

  if (isLoading) {
    return <div className="mt-4 h-16 animate-pulse rounded-xl bg-vektor-surface" />;
  }

  const text = data?.insight?.description;
  if (!text) return null;

  return (
    <div className="mt-4 rounded-xl border border-vektor-border bg-vektor-surface p-4">
      <p className="text-sm leading-7 text-vektor-body">{text}</p>
    </div>
  );
}

function PanelFrame({
  title,
  tooltip,
  controls,
  children,
}: {
  title: string;
  tooltip: string;
  controls?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <article className="vektor-card p-5">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <Tooltip content={tooltip}>
            <h2 className="text-lg font-semibold text-vektor-white">{title}</h2>
          </Tooltip>
          <div className="mt-2 flex items-center gap-3 text-xs text-vektor-muted">
            <Tooltip content="Eje X: el paso del tiempo o la agrupacion que estas comparando.">
              <span>Eje X</span>
            </Tooltip>
            <Tooltip content="Eje Y: el valor monetario o el porcentaje asociado a la metrica elegida.">
              <span>Eje Y</span>
            </Tooltip>
          </div>
        </div>
        {controls ? <div className="w-full lg:w-[240px]">{controls}</div> : null}
      </div>
      <div className="mt-5">{children}</div>
    </article>
  );
}

function ChartSkeleton() {
  return <div className="h-[320px] animate-pulse rounded-2xl bg-vektor-surface" />;
}

export function DashboardAnalysisScreen({ sales, expenses, products, scoreHistory, loading }: Props) {
  const hasTransactionData = sales.length > 0 || expenses.length > 0;

  const [lineMetric, setLineMetric] = useState("caja");
  const [granularity, setGranularity] = useState<"hourly" | "daily" | "weekly" | "monthly">(
    "daily",
  );
  const [compareBy, setCompareBy] = useState("categoria");
  const [distribution, setDistribution] = useState("ventasCategoria");

  const lineData = useMemo(
    () => buildLineSeries(lineMetric as "caja" | "ventas" | "margen" | "stock", sales, expenses, products, granularity, scoreHistory),
    [lineMetric, sales, expenses, products, granularity, scoreHistory],
  );
  const comparisonData = useMemo(
    () => buildComparisonSeries(compareBy as "categoria" | "proveedor" | "metodo" | "dia", sales, expenses, products),
    [compareBy, sales, expenses, products],
  );
  const distributionData = useMemo(
    () => buildDistributionSeries(distribution as "ventasCategoria" | "stockEstado" | "comprasProveedor" | "cajaMetodo", sales, expenses, products),
    [distribution, sales, expenses, products],
  );

  const totalDistribution = distributionData.reduce((sum, item) => sum + item.value, 0);
  const lineConfig = LINE_METRIC_OPTIONS.find((option) => option.value === lineMetric) ?? LINE_METRIC_OPTIONS[0]!;
  const compareConfig = COMPARE_OPTIONS.find((option) => option.value === compareBy) ?? COMPARE_OPTIONS[0]!;
  const distributionConfig =
    DISTRIBUTION_OPTIONS.find((option) => option.value === distribution) ?? DISTRIBUTION_OPTIONS[0]!;

  return (
    <div className="grid gap-4 xl:grid-cols-3">
      {!hasTransactionData && (
        <div className="xl:col-span-3 rounded-xl border border-vektor-border bg-vektor-surface px-5 py-4 text-sm text-vektor-body">
          Los gráficos estarán disponibles cuando cargues ventas o gastos.{" "}
          <Link href="/ingestion" className="font-medium text-vektor-white underline-offset-2 hover:underline">
            Cargar archivo →
          </Link>
        </div>
      )}
      <PanelFrame
        title={lineConfig.title}
        tooltip="Este grafico muestra como fue cambiando la metrica elegida a lo largo del tiempo."
        controls={(
          <div className="space-y-3">
            <Select
              options={LINE_METRIC_OPTIONS.map((option) => ({ value: option.value, label: option.label }))}
              value={lineMetric}
              onChange={setLineMetric}
            />
            <div className="inline-flex rounded-full border border-vektor-border bg-vektor-surface p-1">
              {[
                { value: "hourly", label: "Hoy (horas)" },
                { value: "daily", label: "Diario" },
                { value: "weekly", label: "Semanal" },
                { value: "monthly", label: "Mensual" },
              ].map((option) => (
                <button
                  key={option.value}
                  type="button"
                  onClick={() =>
                    setGranularity(option.value as "hourly" | "daily" | "weekly" | "monthly")
                  }
                  className={[
                    "rounded-full px-3 py-1.5 text-xs font-medium",
                    granularity === option.value
                      ? "bg-vektor-blue text-vektor-white"
                      : "text-vektor-body",
                  ].join(" ")}
                >
                  {option.label}
                </button>
              ))}
            </div>
          </div>
        )}
      >
        {loading ? (
          <ChartSkeleton />
        ) : (
          <>
            <div className="h-[320px]">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={lineData}>
                  <CartesianGrid stroke="rgba(255,255,255,0.08)" vertical={false} />
                  <XAxis dataKey="label" stroke="#90a2bc" tickLine={false} axisLine={false} />
                  <YAxis
                    stroke="#90a2bc"
                    tickLine={false}
                    axisLine={false}
                    tickFormatter={(value) => {
                      if (lineMetric === "margen") return `${Number(value).toFixed(1)}%`;
                      if (lineMetric === "stock") return `${Math.round(Number(value))}`;
                      return formatARSCompact(Number(value));
                    }}
                  />
                  <RechartsTooltip
                    contentStyle={{ background: "#162236", border: "1px solid #243246", borderRadius: 16 }}
                    formatter={(value, _name, item) => {
                      if (lineMetric === "margen") return [`${Number(value ?? 0).toFixed(1)}%`, "Margen"];
                      if (lineMetric === "stock") return [`${Math.round(Number(value ?? 0))} pts`, "Score de stock"];
                      return [
                        `${formatARS(Number(value ?? 0))} (${formatSignedPercent(Number((item as { payload?: { change?: number } })?.payload?.change ?? 0))})`,
                        "Valor",
                      ];
                    }}
                  />
                  <Line type="monotone" dataKey="value" stroke="#3a86ff" strokeWidth={3} dot={{ r: 3 }} connectNulls={false} />
                </LineChart>
              </ResponsiveContainer>
            </div>
            {lineMetric === "stock" && (
              <p className="mt-2 text-xs text-vektor-muted">
                Escala 0–100 · score de salud del inventario calculado por el motor financiero.
              </p>
            )}
            {hasTransactionData && <InsightBlock />}
          </>
        )}
      </PanelFrame>

      <PanelFrame
        title={compareConfig.title}
        tooltip="Este grafico compara grupos entre si para mostrar concentracion, dependencia o diferencia de desempeno."
        controls={(
          <Select
            options={COMPARE_OPTIONS.map((option) => ({ value: option.value, label: option.label }))}
            value={compareBy}
            onChange={setCompareBy}
          />
        )}
      >
        {loading ? (
          <ChartSkeleton />
        ) : (
          <>
            <div className="h-[320px]">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={comparisonData}>
                  <CartesianGrid stroke="rgba(255,255,255,0.08)" vertical={false} />
                  <XAxis dataKey="label" stroke="#90a2bc" tickLine={false} axisLine={false} />
                  <YAxis stroke="#90a2bc" tickLine={false} axisLine={false} tickFormatter={(value) => formatARSCompact(Number(value))} />
                  <RechartsTooltip
                    contentStyle={{ background: "#162236", border: "1px solid #243246", borderRadius: 16 }}
                    formatter={(value) => [formatARS(Number(value ?? 0)), "Valor"]}
                  />
                  <Bar dataKey="value" radius={[10, 10, 0, 0]}>
                    {comparisonData.map((entry, index) => (
                      <Cell key={entry.label} fill={CHART_COLORS[index % CHART_COLORS.length]} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
            {hasTransactionData && <InsightBlock />}
          </>
        )}
      </PanelFrame>

      <PanelFrame
        title={distributionConfig.title}
        tooltip="Este grafico muestra como se reparte una metrica entre sus componentes principales."
        controls={(
          <Select
            options={DISTRIBUTION_OPTIONS.map((option) => ({ value: option.value, label: option.label }))}
            value={distribution}
            onChange={setDistribution}
          />
        )}
      >
        {loading ? (
          <ChartSkeleton />
        ) : (
          <>
            <div className="grid gap-4 lg:grid-cols-[1fr_180px]">
              <div className="relative h-[320px]">
                <ResponsiveContainer width="100%" height="100%">
                  <PieChart>
                    <Pie
                      data={distributionData}
                      dataKey="value"
                      nameKey="name"
                      innerRadius={72}
                      outerRadius={110}
                      paddingAngle={3}
                    >
                      {distributionData.map((entry, index) => (
                        <Cell key={entry.name} fill={CHART_COLORS[index % CHART_COLORS.length]} />
                      ))}
                    </Pie>
                    <RechartsTooltip
                      contentStyle={{ background: "#162236", border: "1px solid #243246", borderRadius: 16 }}
                      formatter={(value) => [formatARS(Number(value ?? 0)), "Valor"]}
                    />
                  </PieChart>
                </ResponsiveContainer>
                <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
                  <span className="text-xs uppercase tracking-[0.16em] text-vektor-muted">Total</span>
                  <span className="mt-2 text-xl font-semibold text-vektor-white">{formatARSCompact(totalDistribution)}</span>
                </div>
              </div>

              <div className="space-y-3">
                {distributionData.map((entry, index) => {
                  const percentage = totalDistribution > 0 ? (entry.value / totalDistribution) * 100 : 0;
                  return (
                    <div key={entry.name} className="flex items-center gap-3 rounded-xl border border-vektor-border bg-vektor-surface p-3">
                      <span
                        className="h-3 w-3 rounded-full"
                        style={{ backgroundColor: CHART_COLORS[index % CHART_COLORS.length] }}
                      />
                      <div className="min-w-0">
                        <p className="truncate text-sm font-medium text-vektor-body">{entry.name}</p>
                        <p className="text-xs text-vektor-muted">{formatPercent(percentage)}</p>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
            {hasTransactionData && <InsightBlock />}
          </>
        )}
      </PanelFrame>

      <BreakdownPanels />
      <ForecastPanel />
    </div>
  );
}

// ── Breakdown panels ─────────────────────────────────────────────────────────

function BreakdownPanels() {
  const { data, isLoading } = useQuery<BusinessBreakdownResponse>({
    queryKey: ["breakdown", 30],
    queryFn: () => fetchBusinessBreakdown(30),
    staleTime: 5 * 60 * 1000,
  });

  if (isLoading) {
    return (
      <>
        <div className="h-[260px] animate-pulse rounded-2xl bg-vektor-surface" />
        <div className="h-[260px] animate-pulse rounded-2xl bg-vektor-surface" />
        <div className="h-[260px] animate-pulse rounded-2xl bg-vektor-surface" />
        <div className="h-[260px] animate-pulse rounded-2xl bg-vektor-surface" />
      </>
    );
  }

  if (!data) return null;

  return (
    <>
      {/* Gastos por categoría */}
      <PanelFrame
        title="Gastos por categoría"
        tooltip="Distribución de egresos del último mes por tipo de gasto."
      >
        {data.expenses_by_category.length === 0 ? (
          <p className="py-8 text-center text-sm text-vk-text-muted">Sin datos de gastos.</p>
        ) : (
          <ul className="space-y-3">
            {data.expenses_by_category.map((item) => (
              <li key={item.category}>
                <div className="flex items-center justify-between text-sm">
                  <span className="text-vk-text-primary">
                    {CATEGORY_LABELS[item.category] ?? item.category}
                  </span>
                  <span className="font-medium text-vk-text-primary">
                    {formatARS(item.total)}
                  </span>
                </div>
                <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-vk-border-w">
                  <div
                    className="h-full rounded-full bg-vk-blue transition-all"
                    style={{ width: `${Math.min(item.pct, 100)}%` }}
                  />
                </div>
                <p className="mt-0.5 text-right text-xs text-vk-text-muted">{item.pct.toFixed(1)}%</p>
              </li>
            ))}
          </ul>
        )}
      </PanelFrame>

      {/* Top proveedores */}
      <PanelFrame
        title="Top proveedores"
        tooltip="Proveedores con mayor peso en los egresos del período."
      >
        {data.top_suppliers.length === 0 ? (
          <p className="py-8 text-center text-sm text-vk-text-muted">Sin proveedores registrados.</p>
        ) : (
          <ul className="space-y-3">
            {data.top_suppliers.map((item) => (
              <li key={item.supplier_name}>
                <div className="flex items-center justify-between text-sm">
                  <span className="truncate pr-3 text-vk-text-primary">{item.supplier_name}</span>
                  <span className="font-medium text-vk-text-primary">{formatARS(item.total)}</span>
                </div>
                <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-vk-border-w">
                  <div
                    className="h-full rounded-full bg-[#27c7b8] transition-all"
                    style={{ width: `${Math.min(item.pct, 100)}%` }}
                  />
                </div>
                <p className="mt-0.5 text-right text-xs text-vk-text-muted">{item.pct.toFixed(1)}%</p>
              </li>
            ))}
          </ul>
        )}
      </PanelFrame>

      {/* Stock crítico */}
      <PanelFrame
        title={`Stock crítico (${data.low_stock_count} / ${data.total_products})`}
        tooltip="Productos por debajo del umbral mínimo de stock."
      >
        {data.low_stock_products.length === 0 ? (
          <p className="py-8 text-center text-sm text-vk-text-muted">
            Todos los productos tienen stock suficiente.
          </p>
        ) : (
          <ul className="space-y-2">
            {data.low_stock_products.map((p) => (
              <li
                key={p.product_id}
                className="flex items-center justify-between rounded-lg border border-vk-border-w px-3 py-2"
              >
                <div>
                  <p className="text-sm font-medium text-vk-text-primary">{p.name}</p>
                  <p className="text-xs text-vk-text-muted">
                    Stock: {p.stock_units} / mínimo {p.low_stock_threshold_units ?? 5}
                  </p>
                </div>
                <span
                  className={`rounded-full px-2 py-0.5 text-xs font-semibold ${
                    p.stock_units === 0
                      ? "bg-vk-danger/15 text-vk-danger"
                      : "bg-amber-500/15 text-amber-400"
                  }`}
                >
                  {p.stock_units === 0 ? "Sin stock" : "Bajo"}
                </span>
              </li>
            ))}
          </ul>
        )}
      </PanelFrame>

      {/* Sin rotación */}
      <PanelFrame
        title={`Sin rotación (${data.no_rotation_count} / ${data.total_products})`}
        tooltip="Productos activos con stock pero sin ventas registradas en el período."
      >
        {data.no_rotation_products.length === 0 ? (
          <p className="py-8 text-center text-sm text-vk-text-muted">
            No hay productos detenidos en este período.
          </p>
        ) : (
          <ul className="space-y-2">
            {data.no_rotation_products.map((p) => (
              <li
                key={p.product_id}
                className="flex items-center justify-between rounded-lg border border-vk-border-w px-3 py-2"
              >
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-vk-text-primary">{p.name}</p>
                  <p className="text-xs text-vk-text-muted">
                    Stock actual: {p.stock_units}
                  </p>
                </div>
                <span className="rounded-full bg-amber-500/15 px-2 py-0.5 text-xs font-semibold text-amber-400">
                  Revisar
                </span>
              </li>
            ))}
          </ul>
        )}
      </PanelFrame>
    </>
  );
}

// ── Panel de forecast de caja ────────────────────────────────────────────────

const TIER_LABELS: Record<number, string> = {
  0: "Sin datos suficientes",
  1: "Proyección básica (14 días)",
  2: "Proyección semanal (30 días)",
  3: "Proyección con tendencia (60 días)",
};

const CONFIDENCE_COLORS: Record<string, string> = {
  HIGH: "text-emerald-400",
  MEDIUM: "text-amber-400",
  LOW: "text-vk-text-muted",
};

function ForecastPanel() {
  const { data, isLoading } = useQuery<CashForecastResponse>({
    queryKey: ["forecast", "cash"],
    queryFn: () => fetchCashForecast(),
    staleTime: 30 * 60 * 1000,
    retry: 1,
  });

  if (isLoading) {
    return <div className="col-span-3 h-[280px] animate-pulse rounded-2xl bg-vektor-surface" />;
  }

  if (!data || data.tier === 0) {
    return (
      <div className="col-span-3 rounded-2xl border border-vk-border-w bg-vk-surface-w p-6">
        <h3 className="text-sm font-semibold text-vk-text-secondary mb-2">Proyección de caja</h3>
        <p className="text-sm text-vk-text-muted">
          {data?.message ?? "Sin datos suficientes para proyectar. Cargá al menos 14 días de ventas y gastos."}
        </p>
      </div>
    );
  }

  const chartData = data.points.map((p) => ({
    date: new Date(p.date + "T00:00:00").toLocaleDateString("es-AR", { day: "2-digit", month: "short" }),
    Ingresos: p.income,
    Egresos: p.expense,
    Neto: p.net,
  }));

  const totalNet = data.points.reduce((s, p) => s + p.net, 0);
  const isPositive = totalNet >= 0;

  return (
    <div className="col-span-3 rounded-2xl border border-vk-border-w bg-vk-surface-w p-6">
      <div className="flex items-start justify-between mb-1">
        <div>
          <h3 className="text-sm font-semibold text-vk-text-secondary">Proyección de caja</h3>
          <p className="text-xs text-vk-text-muted mt-0.5">{TIER_LABELS[data.tier]}</p>
        </div>
        <div className="text-right">
          <p className={`text-xs font-medium ${CONFIDENCE_COLORS[data.confidence] ?? "text-vk-text-muted"}`}>
            Confianza {data.confidence}
          </p>
          <p className="text-xs text-vk-text-muted">{data.data_days} días de historial</p>
        </div>
      </div>

      <div className="mt-1 mb-4">
        <span className={`text-sm font-semibold ${isPositive ? "text-emerald-400" : "text-vk-danger"}`}>
          Neto proyectado: {isPositive ? "+" : ""}{formatARSCompact(totalNet)}
        </span>
        <span className="ml-2 text-xs text-vk-text-muted">en {data.horizon_days} días</span>
      </div>

      <ResponsiveContainer width="100%" height={220}>
        <LineChart data={chartData} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.06)" />
          <XAxis
            dataKey="date"
            tick={{ fontSize: 10, fill: "var(--vektor-muted)" }}
            tickLine={false}
            interval={Math.ceil(chartData.length / 6)}
          />
          <YAxis
            tick={{ fontSize: 10, fill: "var(--vektor-muted)" }}
            tickFormatter={(v: number) => formatARSCompact(v)}
            tickLine={false}
            axisLine={false}
            width={60}
          />
          <RechartsTooltip
            contentStyle={{ background: "var(--vektor-surface)", border: "1px solid var(--vektor-border)", borderRadius: 8, fontSize: 12 }}
            formatter={(v) => formatARS(Number(v ?? 0))}
          />
          <Line type="monotone" dataKey="Ingresos" stroke="#3a86ff" strokeWidth={2} dot={false} />
          <Line type="monotone" dataKey="Egresos" stroke="#f06c79" strokeWidth={2} dot={false} />
          <Line type="monotone" dataKey="Neto" stroke="#27c7b8" strokeWidth={1.5} strokeDasharray="4 2" dot={false} />
        </LineChart>
      </ResponsiveContainer>

      <div className="mt-3 flex gap-4 text-xs text-vk-text-muted">
        <span className="flex items-center gap-1.5"><span className="h-2 w-4 rounded bg-vk-blue" />Ingresos</span>
        <span className="flex items-center gap-1.5"><span className="h-2 w-4 rounded bg-vk-danger" />Egresos</span>
        <span className="flex items-center gap-1.5"><span className="h-2 w-4 rounded bg-[#27c7b8]" />Neto</span>
      </div>
    </div>
  );
}
