"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronDown } from "lucide-react";
import { fetchBusinessBreakdown, fetchCurrentInsight } from "@/services/dashboard.service";
import type {
  BusinessBreakdownResponse,
  HealthScoreV2Response,
  SupplierPurchaseBreakdownItem,
} from "@/types/api";
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
import { categoryDisplay } from "@/lib/expenseCategories";
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
  explanation,
  controls,
  children,
}: {
  title: string;
  tooltip: string;
  /** Explicación visible: qué muestra el gráfico y cómo leerlo. */
  explanation?: string;
  controls?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <article className="vektor-card p-5">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0 lg:max-w-[70%]">
          <Tooltip content={tooltip}>
            <h2 className="text-lg font-semibold text-vektor-white">{title}</h2>
          </Tooltip>
          {explanation ? (
            <p className="mt-1.5 text-sm leading-6 text-vektor-muted">{explanation}</p>
          ) : null}
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
    <div className="space-y-6">
      {!hasTransactionData && (
        <div className="rounded-xl border border-vektor-border bg-vektor-surface px-5 py-4 text-sm text-vektor-body">
          Los gráficos estarán disponibles cuando cargues ventas o gastos.{" "}
          <Link href="/ingestion" className="font-medium text-vektor-white underline-offset-2 hover:underline">
            Cargar archivo →
          </Link>
        </div>
      )}
      {/* Gráficos principales: apilados a ancho completo para mejor legibilidad */}
      <div className="space-y-6">
      <PanelFrame
        title={lineConfig.title}
        tooltip="Este grafico muestra como fue cambiando la metrica elegida a lo largo del tiempo."
        explanation="Cada punto es un período (día, semana o mes según elijas). Si la línea sube, la métrica mejora; si baja sostenidamente, conviene prestar atención. El eje horizontal es el tiempo y el vertical, el valor."
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
                  <XAxis dataKey="label" stroke="#d6e2f0" tickLine={false} axisLine={false} />
                  <YAxis
                    stroke="#d6e2f0"
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
        explanation="Cada barra es un grupo (categoría, proveedor, método de pago o día). Cuanto más alta la barra, más concentra. Te sirve para ver de qué dependés y dónde está el grueso del movimiento."
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
                  <XAxis dataKey="label" stroke="#d6e2f0" tickLine={false} axisLine={false} />
                  <YAxis stroke="#d6e2f0" tickLine={false} axisLine={false} tickFormatter={(value) => formatARSCompact(Number(value))} />
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
        explanation="El anillo muestra cómo se reparte el total entre sus partes. Cada porción es el peso de ese componente sobre el total (en el centro). Las porciones más grandes son las que más pesan."
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
      </div>

      {/* Desgloses por categoría, proveedores y stock: dos columnas */}
      <div className="grid gap-4 xl:grid-cols-2">
        <BreakdownPanels />
      </div>
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
      {/* Inversión en mercadería: balance compra → stock (FASE D) */}
      <PanelFrame
        title="Inversión en mercadería"
        tooltip="Lo que salió de caja en compras de mercadería (COGS) del período y el valor actual del stock. El gasto operativo (OPEX) se muestra como referencia."
        explanation="Cuánto pusiste en comprar mercadería y cuánto vale hoy tu stock. Si el valor del stock es mucho mayor que la salida de caja, tenés plata inmovilizada en góndola."
      >
        <ul className="space-y-4 py-2">
          <li className="flex items-center justify-between text-sm">
            <span className="text-vektor-white">Salida de caja en mercadería</span>
            <span className="font-medium text-vektor-white">
              {formatARS(data.cogs_total)}
            </span>
          </li>
          <li className="flex items-center justify-between text-sm">
            <span className="text-vektor-white">Valor actual del stock</span>
            <span className="font-medium text-vektor-white">
              {formatARS(data.stock_value_total)}
            </span>
          </li>
          <li className="flex items-center justify-between border-t border-vektor-border pt-3 text-sm">
            <span className="text-vektor-muted">Gastos operativos del período</span>
            <span className="text-vektor-muted">{formatARS(data.opex_total)}</span>
          </li>
        </ul>
      </PanelFrame>

      {/* Gastos por categoría */}
      <PanelFrame
        title="Gastos por categoría"
        tooltip="Distribución de egresos del último mes por tipo de gasto."
        explanation="En qué se te va la plata. La barra y el porcentaje muestran cuánto pesa cada categoría sobre el total de gastos del período."
      >
        {data.expenses_by_category.length === 0 ? (
          <p className="py-8 text-center text-sm text-vektor-muted">Sin datos de gastos.</p>
        ) : (
          <ul className="space-y-3">
            {data.expenses_by_category.map((item) => (
              <li key={item.category}>
                <div className="flex items-center justify-between text-sm">
                  <span className="text-vektor-white">
                    {categoryDisplay(item.category)}
                  </span>
                  <span className="font-medium text-vektor-white">
                    {formatARS(item.total)}
                  </span>
                </div>
                <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-vektor-border">
                  <div
                    className="h-full rounded-full bg-vektor-blue transition-all"
                    style={{ width: `${Math.min(item.pct, 100)}%` }}
                  />
                </div>
                <p className="mt-0.5 text-right text-xs text-vektor-muted">{item.pct.toFixed(1)}%</p>
              </li>
            ))}
          </ul>
        )}
      </PanelFrame>

      {/* Top proveedores — acordeón por proveedor real */}
      <PanelFrame
        title="Top proveedores"
        tooltip="Compras de mercadería acumuladas (histórico) agrupadas por proveedor real. Desplegá cada uno para ver qué productos te provee."
        explanation="A quién le comprás más mercadería desde que cargás compras. Si uno solo concentra casi todo, dependés demasiado de él: conviene tener una alternativa. Tocá un proveedor para ver sus productos."
      >
        <SuppliersBreakdown suppliers={data.suppliers_breakdown} />
      </PanelFrame>

      {/* Stock crítico */}
      <PanelFrame
        title={`Stock crítico (${data.low_stock_count} / ${data.total_products})`}
        tooltip="Productos por debajo del umbral mínimo de stock."
        explanation="Productos que están por debajo de tu umbral mínimo. Si no los reponés pronto, podés quedarte sin venderlos."
      >
        {data.low_stock_products.length === 0 ? (
          <p className="py-8 text-center text-sm text-vektor-muted">
            Todos los productos tienen stock suficiente.
          </p>
        ) : (
          <ul className="space-y-2">
            {data.low_stock_products.map((p) => (
              <li
                key={p.product_id}
                className="flex items-center justify-between rounded-lg border border-vektor-border px-3 py-2"
              >
                <div>
                  <p className="text-sm font-medium text-vektor-white">{p.name}</p>
                  <p className="text-xs text-vektor-muted">
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
        explanation="Productos con stock pero sin ninguna venta en el período: plata parada. Conviene impulsarlos con una promo o discontinuarlos."
      >
        {data.no_rotation_products.length === 0 ? (
          <p className="py-8 text-center text-sm text-vektor-muted">
            No hay productos detenidos en este período.
          </p>
        ) : (
          <div className="space-y-3">
            {/* Cifra-tesis: la plata parada es el dato que importa. */}
            <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3">
              <p className="text-xs uppercase tracking-widest text-amber-300/80">Plata parada</p>
              <p className="mt-0.5 font-display text-2xl font-bold tabular-nums text-vektor-amber">
                {formatARS(data.no_rotation_value_total)}
              </p>
              <p className="mt-1 text-xs text-vektor-muted">
                Inmovilizada en {data.no_rotation_count} producto
                {data.no_rotation_count === 1 ? "" : "s"} sin ventas en el período.
                {data.no_rotation_unvalued_count > 0
                  ? ` ${data.no_rotation_unvalued_count} sin costo cargado, no valuados.`
                  : ""}
              </p>
            </div>
            <ul className="space-y-2">
              {data.no_rotation_products.map((p) => (
                <li
                  key={p.product_id}
                  className="flex items-center justify-between rounded-lg border border-vektor-border px-3 py-2"
                >
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium text-vektor-white">{p.name}</p>
                    <p className="text-xs text-vektor-muted">Stock actual: {p.stock_units}</p>
                  </div>
                  <span className="ml-3 shrink-0 text-sm font-medium tabular-nums text-vektor-amber">
                    {p.immobilized_value > 0 ? formatARS(p.immobilized_value) : "sin costo"}
                  </span>
                </li>
              ))}
            </ul>
            {data.no_rotation_count > data.no_rotation_products.length ? (
              <p className="text-center text-xs text-vektor-muted">
                Mostrando los {data.no_rotation_products.length} de mayor valor, de{" "}
                {data.no_rotation_count}.
              </p>
            ) : null}
          </div>
        )}
      </PanelFrame>
    </>
  );
}

// ── Acordeón de proveedores ───────────────────────────────────────────────────

function SuppliersBreakdown({ suppliers }: { suppliers: SupplierPurchaseBreakdownItem[] }) {
  if (suppliers.length === 0) {
    return (
      <p className="py-8 text-center text-sm text-vektor-muted">
        Sin compras de mercadería en el período.
      </p>
    );
  }
  const maxTotal = Math.max(...suppliers.map((s) => s.total_purchased), 1);
  return (
    <ul className="space-y-2">
      {suppliers.map((s) => (
        <SupplierAccordionRow
          key={s.supplier_id ?? "__unassigned__"}
          supplier={s}
          maxTotal={maxTotal}
        />
      ))}
    </ul>
  );
}

function SupplierAccordionRow({
  supplier,
  maxTotal,
}: {
  supplier: SupplierPurchaseBreakdownItem;
  maxTotal: number;
}) {
  const [open, setOpen] = useState(false);
  const sharePct = Math.min((supplier.total_purchased / maxTotal) * 100, 100);
  const hasProducts = supplier.products.length > 0;

  return (
    <li className="rounded-lg border border-vektor-border">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        disabled={!hasProducts}
        className="w-full px-3 py-2 text-left transition-colors hover:bg-vektor-surface/60 disabled:cursor-default"
      >
        <div className="flex items-center justify-between gap-3 text-sm">
          <span className="flex min-w-0 items-center gap-1.5">
            {hasProducts ? (
              <ChevronDown
                className={`h-3.5 w-3.5 shrink-0 text-vektor-muted transition-transform motion-reduce:transition-none ${
                  open ? "rotate-180" : ""
                }`}
              />
            ) : (
              <span className="inline-block w-3.5" />
            )}
            <span className="truncate text-vektor-white">{supplier.supplier_name}</span>
          </span>
          <span className="shrink-0 font-medium tabular-nums text-vektor-white">
            {formatARS(supplier.total_purchased)}
          </span>
        </div>
        <div className="ml-5 mt-1 h-1.5 w-[calc(100%-1.25rem)] overflow-hidden rounded-full bg-vektor-border">
          <div
            className="h-full rounded-full bg-[#27c7b8] transition-all motion-reduce:transition-none"
            style={{ width: `${sharePct}%` }}
          />
        </div>
        {supplier.coverage_pct < 100 ? (
          <p className="ml-5 mt-1 text-xs text-vektor-muted">
            Calculado sobre {supplier.coverage_pct.toFixed(0)}% de las compras (resto sin costo
            cargado).
          </p>
        ) : null}
      </button>

      {open && hasProducts ? (
        // Regla vertical hairline: ata los productos a su proveedor.
        <ul className="ml-5 space-y-1.5 border-l border-vektor-border py-2 pl-3 pr-3">
          {supplier.products.map((p) => (
            <li key={p.product_id} className="flex items-baseline justify-between gap-3 text-sm">
              <span className="min-w-0">
                <span className="block truncate text-vektor-body">{p.name}</span>
                <span className="block truncate text-xs text-vektor-muted">{p.brand}</span>
              </span>
              <span className="shrink-0 font-medium tabular-nums text-vektor-white">
                {formatARS(p.total_amount)}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </li>
  );
}
