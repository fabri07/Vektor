"use client";

import { useQuery } from "@tanstack/react-query";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip as RechartsTooltip,
  XAxis,
  YAxis,
} from "recharts";
import { fetchCashForecast } from "@/services/dashboard.service";
import type { CashForecastResponse } from "@/types/api";
import { formatARS, formatARSCompact } from "@/features/dashboard/dashboardData";

const TIER_LABELS: Record<number, string> = {
  0: "Sin datos suficientes",
  1: "Proyección básica (14 días)",
  2: "Proyección semanal (30 días)",
  3: "Proyección con tendencia (60 días)",
};

const CONFIDENCE_COLORS: Record<string, string> = {
  HIGH: "text-emerald-400",
  MEDIUM: "text-amber-400",
  LOW: "text-vektor-muted",
};

/**
 * Proyección de caja (ingresos/egresos/neto futuros). Autocontenido:
 * trae su propia data vía fetchCashForecast. Vive en la pestaña Balance.
 */
export function ForecastPanel() {
  const { data, isLoading } = useQuery<CashForecastResponse>({
    queryKey: ["forecast", "cash"],
    queryFn: () => fetchCashForecast(),
    staleTime: 30 * 60 * 1000,
    retry: 1,
  });

  if (isLoading) {
    return <div className="h-[280px] animate-pulse rounded-2xl bg-vektor-surface" />;
  }

  if (!data || data.tier === 0) {
    return (
      <div className="rounded-2xl border border-vektor-border bg-vektor-ink p-6">
        <h3 className="text-sm font-semibold text-vektor-body mb-2">Proyección de caja</h3>
        <p className="text-sm text-vektor-muted">
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
    <div className="rounded-2xl border border-vektor-border bg-vektor-ink p-6">
      <div className="flex items-start justify-between mb-1">
        <div>
          <h3 className="text-sm font-semibold text-vektor-body">Proyección de caja</h3>
          <p className="text-xs text-vektor-muted mt-0.5">{TIER_LABELS[data.tier]}</p>
        </div>
        <div className="text-right">
          <p className={`text-xs font-medium ${CONFIDENCE_COLORS[data.confidence] ?? "text-vektor-muted"}`}>
            Confianza {data.confidence}
          </p>
          <p className="text-xs text-vektor-muted">{data.data_days} días de historial</p>
        </div>
      </div>

      <p className="mt-1 text-sm leading-6 text-vektor-muted">
        Estimación de cómo va a entrar y salir la plata en los próximos días, según tu
        historial. La línea azul son ingresos, la roja egresos y la verde punteada el neto
        (lo que te queda). Es una proyección, no una certeza.
      </p>

      <div className="mt-3 mb-4">
        <span className={`text-sm font-semibold ${isPositive ? "text-emerald-400" : "text-vk-danger"}`}>
          Neto proyectado: {isPositive ? "+" : ""}{formatARSCompact(totalNet)}
        </span>
        <span className="ml-2 text-xs text-vektor-muted">en {data.horizon_days} días</span>
      </div>

      <ResponsiveContainer width="100%" height={220}>
        <LineChart data={chartData} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.06)" />
          <XAxis
            dataKey="date"
            tick={{ fontSize: 10, fill: "var(--vektor-body)" }}
            tickLine={false}
            interval={Math.ceil(chartData.length / 6)}
          />
          <YAxis
            tick={{ fontSize: 10, fill: "var(--vektor-body)" }}
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

      <div className="mt-3 flex gap-4 text-xs text-vektor-muted">
        <span className="flex items-center gap-1.5"><span className="h-2 w-4 rounded bg-vektor-blue" />Ingresos</span>
        <span className="flex items-center gap-1.5"><span className="h-2 w-4 rounded bg-vk-danger" />Egresos</span>
        <span className="flex items-center gap-1.5"><span className="h-2 w-4 rounded bg-[#27c7b8]" />Neto</span>
      </div>
    </div>
  );
}
