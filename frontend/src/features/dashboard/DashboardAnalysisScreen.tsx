"use client";

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
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

function InsightBlock({ metric, period }: { metric: string; period: string }) {
  const { data, isLoading } = useQuery<{ insight: string }>({
    queryKey: ["analysis-insight", metric, period],
    queryFn: async () => {
      const response = await fetch(`/api/analisis/insight?metric=${metric}&period=${period}`);
      if (!response.ok) throw new Error("No se pudo cargar el insight.");
      return (await response.json()) as { insight: string };
    },
    staleTime: 60 * 60 * 1000,
  });

  if (isLoading) {
    return <div className="mt-4 h-16 animate-pulse rounded-xl bg-vektor-surface" />;
  }

  return (
    <div className="mt-4 rounded-xl border border-vektor-border bg-vektor-surface p-4">
      <p className="text-sm leading-7 text-vektor-body">{data?.insight}</p>
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

export function DashboardAnalysisScreen({ sales, expenses, products, loading }: Props) {
  const [lineMetric, setLineMetric] = useState("caja");
  const [granularity, setGranularity] = useState<"daily" | "weekly" | "monthly">("daily");
  const [compareBy, setCompareBy] = useState("categoria");
  const [distribution, setDistribution] = useState("ventasCategoria");

  const lineData = useMemo(
    () => buildLineSeries(lineMetric as "caja" | "ventas" | "margen" | "stock", sales, expenses, products, granularity),
    [lineMetric, sales, expenses, products, granularity],
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
                { value: "daily", label: "Diario" },
                { value: "weekly", label: "Semanal" },
                { value: "monthly", label: "Mensual" },
              ].map((option) => (
                <button
                  key={option.value}
                  type="button"
                  onClick={() => setGranularity(option.value as "daily" | "weekly" | "monthly")}
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
                  <YAxis stroke="#90a2bc" tickLine={false} axisLine={false} tickFormatter={(value) => formatARSCompact(Number(value))} />
                  <RechartsTooltip
                    contentStyle={{ background: "#162236", border: "1px solid #243246", borderRadius: 16 }}
                    formatter={(value, _name, item) => [
                      `${formatARS(Number(value ?? 0))} (${formatSignedPercent(Number((item as { payload?: { change?: number } })?.payload?.change ?? 0))})`,
                      "Valor",
                    ]}
                  />
                  <Line type="monotone" dataKey="value" stroke="#3a86ff" strokeWidth={3} dot={{ r: 3 }} />
                </LineChart>
              </ResponsiveContainer>
            </div>
            <InsightBlock metric={lineMetric} period={granularity} />
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
            <InsightBlock metric={compareBy} period="comparacion" />
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
            <InsightBlock metric={distribution} period="distribucion" />
          </>
        )}
      </PanelFrame>
    </div>
  );
}
