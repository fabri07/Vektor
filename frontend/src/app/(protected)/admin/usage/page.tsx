"use client";

import { useState } from "react";
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
import { PageWrapper } from "@/components/layout/PageWrapper";
import { StatCard } from "@/components/ui/StatCard";
import { EmptyState } from "@/components/ui/EmptyState";
import { Select } from "@/components/ui/Select";
import { SmartTable } from "@/components/ui/SmartTable";
import type { SmartColumn } from "@/components/ui/SmartTable";
import { useAuthStore } from "@/stores/authStore";
import { usageService } from "@/services/usage.service";
import type {
  UsageByAgent,
  UsageByModel,
  UsageByTenant,
} from "@/services/usage.service";

const PERIOD_OPTIONS = [
  { value: "7", label: "Últimos 7 días" },
  { value: "30", label: "Últimos 30 días" },
  { value: "90", label: "Últimos 90 días" },
];

/** Formatea un monto en USD como `$X.XX`. */
function fmtUsd(value: number): string {
  const safe = Number.isFinite(value) ? value : 0;
  return `$${safe.toFixed(2)}`;
}

/** Formatea un entero con separador de miles (es-AR). */
function fmtInt(value: number): string {
  const safe = Number.isFinite(value) ? value : 0;
  return safe.toLocaleString("es-AR");
}

const agentColumns: SmartColumn<UsageByAgent>[] = [
  { key: "agent", header: "Agente" },
  {
    key: "tokens_total",
    header: "Tokens",
    render: (v) => fmtInt(Number(v ?? 0)),
    csvValue: (v) => String(Number(v ?? 0)),
  },
  {
    key: "cost_usd",
    header: "Costo (USD)",
    render: (v) => fmtUsd(Number(v ?? 0)),
    csvValue: (v) => Number(v ?? 0).toFixed(4),
  },
];

const modelColumns: SmartColumn<UsageByModel>[] = [
  {
    key: "model",
    header: "Modelo",
    render: (v, row) => (
      <span className="flex items-center gap-2">
        <span>{String(v ?? "—")}</span>
        {!row.priced && (
          <span className="inline-flex items-center rounded-full bg-vk-surface-2 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-vk-text-muted">
            sin precio
          </span>
        )}
      </span>
    ),
  },
  {
    key: "tokens_input",
    header: "Input",
    render: (v) => fmtInt(Number(v ?? 0)),
    csvValue: (v) => String(Number(v ?? 0)),
  },
  {
    key: "tokens_output",
    header: "Output",
    render: (v) => fmtInt(Number(v ?? 0)),
    csvValue: (v) => String(Number(v ?? 0)),
  },
  {
    key: "cost_usd",
    header: "Costo (USD)",
    render: (v) => fmtUsd(Number(v ?? 0)),
    csvValue: (v) => Number(v ?? 0).toFixed(4),
  },
];

const tenantColumns: SmartColumn<UsageByTenant>[] = [
  { key: "tenant_id", header: "Tenant" },
  {
    key: "tokens_total",
    header: "Tokens",
    render: (v) => fmtInt(Number(v ?? 0)),
    csvValue: (v) => String(Number(v ?? 0)),
  },
  {
    key: "cost_usd",
    header: "Costo (USD)",
    render: (v) => fmtUsd(Number(v ?? 0)),
    csvValue: (v) => Number(v ?? 0).toFixed(4),
  },
];

export default function AdminUsagePage() {
  const user = useAuthStore((s) => s.user);
  const isSuperadmin = user?.role === "SUPERADMIN";
  const [days, setDays] = useState(30);

  const { data, isLoading, isError } = useQuery({
    queryKey: ["admin-usage", days],
    queryFn: () => usageService.getUsage(days),
    enabled: isSuperadmin,
    staleTime: 5 * 60 * 1000,
  });

  if (!isSuperadmin) {
    return (
      <PageWrapper title="Uso & costos">
        <div className="rounded-lg border border-vk-border-w bg-vk-surface-w p-10">
          <EmptyState
            title="Acceso restringido (SUPERADMIN)"
            description="Esta sección de consumo de tokens y costos solo está disponible para administradores de la plataforma."
          />
        </div>
      </PageWrapper>
    );
  }

  // Parsing defensivo del shape — toleramos listas/totales faltantes.
  const totals = data?.totals ?? {
    tokens_input: 0,
    tokens_output: 0,
    tokens_total: 0,
    cost_usd: 0,
    decisions: 0,
  };
  const byAgent: UsageByAgent[] = Array.isArray(data?.by_agent) ? data.by_agent : [];
  const byModel: UsageByModel[] = Array.isArray(data?.by_model) ? data.by_model : [];
  const byTenant: UsageByTenant[] = Array.isArray(data?.by_tenant) ? data.by_tenant : [];
  const byDay = Array.isArray(data?.by_day) ? data.by_day : [];

  const chartData = byDay.map((d) => ({
    label: d.date,
    cost: Number.isFinite(d.cost_usd) ? d.cost_usd : 0,
  }));

  const periodSelector = (
    <div className="w-48">
      <Select
        options={PERIOD_OPTIONS}
        value={String(days)}
        onChange={(v) => setDays(Number(v))}
      />
    </div>
  );

  return (
    <PageWrapper title="Uso & costos" actions={periodSelector}>
      {isLoading ? (
        <div className="rounded-lg border border-vk-border-w bg-vk-surface-w p-10 text-center text-sm text-vk-text-muted">
          Cargando consumo…
        </div>
      ) : isError ? (
        <div className="rounded-lg border border-vk-border-w bg-vk-surface-w p-10">
          <EmptyState
            title="No se pudo cargar el consumo"
            description="Ocurrió un error al consultar el dashboard de uso. Intentá nuevamente."
          />
        </div>
      ) : totals.tokens_total === 0 ? (
        <div className="rounded-lg border border-vk-border-w bg-vk-surface-w p-10">
          <EmptyState
            title="Sin consumo en el período"
            description="No se registró consumo de tokens en la ventana seleccionada."
          />
        </div>
      ) : (
        <>
          {/* StatCards */}
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            <StatCard label="Tokens totales" value={fmtInt(totals.tokens_total)} />
            <StatCard label="Costo total (USD)" value={fmtUsd(totals.cost_usd)} />
            <StatCard label="Decisiones" value={fmtInt(totals.decisions)} />
            <StatCard label="Tokens input" value={fmtInt(totals.tokens_input)} />
            <StatCard label="Tokens output" value={fmtInt(totals.tokens_output)} />
          </div>

          {/* Chart: costo por día */}
          <div className="rounded-lg border border-vk-border-w bg-vk-surface-w p-6 shadow-vk-sm">
            <h2 className="mb-4 text-sm font-semibold text-vk-text-primary">
              Costo por día (USD)
            </h2>
            <div className="h-[280px]">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={chartData} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                  <CartesianGrid stroke="rgba(255,255,255,0.08)" vertical={false} />
                  <XAxis dataKey="label" stroke="#90a2bc" tickLine={false} axisLine={false} />
                  <YAxis
                    stroke="#90a2bc"
                    tickLine={false}
                    axisLine={false}
                    tickFormatter={(value) => fmtUsd(Number(value))}
                  />
                  <RechartsTooltip
                    contentStyle={{
                      background: "#162236",
                      border: "1px solid #243246",
                      borderRadius: 16,
                    }}
                    formatter={(value) => [fmtUsd(Number(value ?? 0)), "Costo"]}
                  />
                  <Line
                    type="monotone"
                    dataKey="cost"
                    stroke="#3a86ff"
                    strokeWidth={2}
                    dot={{ r: 3 }}
                    connectNulls={false}
                  />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </div>

          {/* Tabla: por agente (top consumers) */}
          <div className="rounded-lg border border-vk-border-w bg-vk-surface-w p-6 shadow-vk-sm">
            <h2 className="mb-4 text-sm font-semibold text-vk-text-primary">
              Consumo por agente
            </h2>
            <SmartTable
              columns={agentColumns}
              data={byAgent}
              exportFilename="vektor-uso-agentes"
              emptyMessage="Sin consumo por agente."
            />
          </div>

          {/* Tabla: por modelo */}
          <div className="rounded-lg border border-vk-border-w bg-vk-surface-w p-6 shadow-vk-sm">
            <h2 className="mb-4 text-sm font-semibold text-vk-text-primary">
              Consumo por modelo
            </h2>
            <SmartTable
              columns={modelColumns}
              data={byModel}
              exportFilename="vektor-uso-modelos"
              emptyMessage="Sin consumo por modelo."
            />
          </div>

          {/* Tabla: por tenant */}
          <div className="rounded-lg border border-vk-border-w bg-vk-surface-w p-6 shadow-vk-sm">
            <h2 className="mb-4 text-sm font-semibold text-vk-text-primary">
              Consumo por tenant
            </h2>
            <SmartTable
              columns={tenantColumns}
              data={byTenant}
              exportFilename="vektor-uso-tenants"
              emptyMessage="Sin consumo por tenant."
            />
          </div>
        </>
      )}
    </PageWrapper>
  );
}
