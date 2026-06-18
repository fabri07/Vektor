"use client";

import { useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { StatCard } from "@/components/ui/StatCard";
import { EmptyState } from "@/components/ui/EmptyState";
import { PeriodFilter } from "@/components/ui/PeriodFilter";
import { ForecastPanel } from "@/features/dashboard/ForecastPanel";
import { type PeriodValue, resolvePeriod } from "@/lib/period";
import { economicSummaryService } from "@/services/economic-summary.service";

function formatARS(value: number): string {
  return new Intl.NumberFormat("es-AR", {
    style: "currency",
    currency: "ARS",
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  }).format(value);
}

/** Pantalla del resumen económico (pestaña Balance del dashboard). */
export function EconomicSummaryScreen() {
  const [period, setPeriod] = useState<PeriodValue>({
    kind: "preset",
    preset: "this_month",
  });
  const { from, to } = resolvePeriod(period);

  const { data, isLoading, isError } = useQuery({
    queryKey: ["economic-summary", from, to],
    queryFn: () => economicSummaryService.getSummary({ from_date: from, to_date: to }),
    staleTime: 60 * 1000,
  });

  const resultTrend = data
    ? data.net_result_ars > 0
      ? "up"
      : data.net_result_ars < 0
        ? "down"
        : "neutral"
    : "neutral";

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <PeriodFilter value={period} onChange={setPeriod} availableRange={null} />
      </div>

      {/* Aclaración de scope (analítico, no contable) */}
      <p className="rounded-lg border border-vk-border-w bg-vk-surface-w px-4 py-2 text-xs text-vk-text-secondary">
        Resumen económico analítico. No reemplaza estados contables certificados. Para
        AFIP/IGJ/terceros, consultá a tu contador.
      </p>

      {isLoading ? (
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          {[...Array<number>(4)].map((_, i) => (
            <div
              key={i}
              className="h-24 animate-pulse rounded-lg border border-vk-border-w bg-vk-surface-w"
            />
          ))}
        </div>
      ) : isError ? (
        <p className="rounded-lg border border-vk-danger/20 bg-vk-danger-bg px-4 py-3 text-sm text-vk-danger">
          Error al cargar el resumen. Recargá la página.
        </p>
      ) : !data || !data.has_data ? (
        <EmptyState
          title="Sin datos en este período"
          description="Cargá ventas, gastos o productos para ver tu resumen económico."
          action={{ label: "Ir al chat", href: "/chat" }}
        />
      ) : (
        <>
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
            <StatCard label="Ingresos" value={formatARS(data.total_income_ars)} />
            <StatCard label="Egresos" value={formatARS(data.total_expenses_ars)} />
            <StatCard
              label="Resultado"
              value={formatARS(data.net_result_ars)}
              trend={resultTrend}
              trendValue={data.net_result_ars >= 0 ? "ganancia" : "pérdida"}
            />
            <StatCard
              label="Stock valorizado"
              value={formatARS(data.stock_value_ars)}
              sublabel="a costo"
            />
          </div>

          {data.missing_cost_count > 0 && (
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-vk-warning/30 bg-vk-warning-bg/40 px-4 py-2">
              <p className="text-xs text-vk-text-secondary">
                {data.missing_cost_count} producto(s) sin costo cargado (
                {data.missing_cost_stock_units} unidades) no se incluyen en el stock
                valorizado. Cargá el costo para una valuación completa.
              </p>
              <Link
                href="/products?filter=requires_completion"
                className="shrink-0 rounded-lg border border-vk-warning/40 bg-vk-surface-w px-3 py-1.5 text-xs font-medium text-vk-text-primary transition-colors hover:bg-vk-bg-light"
              >
                Completar costos
              </Link>
            </div>
          )}
        </>
      )}

      {/* Proyección de caja (movida desde Análisis) */}
      <ForecastPanel />

      {/* Disclaimer legal — al pie, en negrita; blanco sobre el fondo oscuro de Véktor */}
      <p className="mt-6 border-t border-vk-border-w pt-4 text-center text-xs font-bold text-vk-text-primary">
        Esto no es un balance contable oficial. Para mayor seguridad, consultá con un
        profesional calificado en la materia.
      </p>
    </div>
  );
}
