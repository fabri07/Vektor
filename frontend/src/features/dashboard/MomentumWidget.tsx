"use client";

import { useQuery } from "@tanstack/react-query";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import { fetchMomentumProfile } from "@/services/momentum.service";
import { Badge } from "@/components/ui/Badge";
import type { MomentumProfileResponse, WeeklyHistoryItem } from "@/types/api";

// ── Helpers ───────────────────────────────────────────────────────────────────

function formatARS(value: number): string {
  return new Intl.NumberFormat("es-AR", {
    style: "currency",
    currency: "ARS",
    maximumFractionDigits: 0,
  }).format(value);
}

function formatWeekLabel(isoDate: string): string {
  const d = new Date(isoDate + "T00:00:00");
  return d.toLocaleDateString("es-AR", { day: "2-digit", month: "2-digit" });
}

// ── Sub-components ────────────────────────────────────────────────────────────

function DeltaIndicator({ delta }: { delta: number | null }) {
  if (delta === null || delta === undefined) return null;

  if (delta > 0) {
    return (
      <span className="text-sm font-medium text-emerald-600">
        ↑ +{delta} vs semana pasada
      </span>
    );
  }
  if (delta < 0) {
    return (
      <span className="text-sm font-medium text-red-500">
        ↓ {delta} vs semana pasada
      </span>
    );
  }
  return (
    <span className="text-sm font-medium text-gray-400">→ sin cambios</span>
  );
}

function ScoreChart({ history }: { history: WeeklyHistoryItem[] }) {
  const data = history.map((h) => ({
    label: formatWeekLabel(h.week_start),
    score: h.avg_score,
  }));

  return (
    <div className="mt-4 h-28">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 4, right: 4, left: -24, bottom: 0 }}>
          <XAxis
            dataKey="label"
            tick={{ fontSize: 10, fill: "#9ca3af" }}
            axisLine={false}
            tickLine={false}
          />
          <YAxis
            domain={[0, 100]}
            tick={{ fontSize: 10, fill: "#9ca3af" }}
            axisLine={false}
            tickLine={false}
          />
          <Tooltip
            contentStyle={{
              fontSize: 12,
              background: "#fff",
              border: "1px solid #e5e7eb",
              borderRadius: 8,
            }}
            formatter={(v) => [v, "Score"]}
          />
          <Line
            type="monotone"
            dataKey="score"
            stroke="#2B7FD4"
            strokeWidth={2}
            dot={{ r: 3, fill: "#2B7FD4" }}
            activeDot={{ r: 5, fill: "#2B7FD4", stroke: "#2B7FD4" }}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function Block1Score({ data }: { data: MomentumProfileResponse }) {
  const latest = data.weekly_history[data.weekly_history.length - 1];
  const currentScore = latest?.avg_score ?? data.best_score_ever;
  const delta = latest?.delta ?? null;
  const isBestEver =
    data.best_score_ever !== null &&
    currentScore !== null &&
    currentScore >= data.best_score_ever &&
    data.best_score_ever > 0;

  return (
    <div>
      <div className="flex items-end gap-3">
        <span className="font-bold leading-none text-vk-text-primary" style={{ fontSize: 64 }}>
          {currentScore != null ? Math.round(currentScore) : "—"}
        </span>
        <span className="mb-2 text-2xl font-light text-gray-400">/ 100</span>
        {isBestEver && (
          <span className="mb-2 text-sm font-semibold text-amber-500">
            ⭐ Tu mejor semana histórica
          </span>
        )}
      </div>
      <DeltaIndicator delta={delta} />
      {data.weekly_history.length > 0 && (
        <ScoreChart history={data.weekly_history} />
      )}
    </div>
  );
}

function Block2Goal({ data }: { data: MomentumProfileResponse }) {
  if (!data.active_goal) return null;
  const { goal, action, estimated_delta, estimated_weeks } = data.active_goal;

  return (
    <div className="rounded-lg border border-gray-100 bg-gray-50 p-4">
      <p className="mb-1 text-xs font-medium uppercase tracking-widest text-gray-400">
        Meta activa
      </p>
      <p className="font-semibold text-gray-800">{goal}</p>
      <p className="mt-1 text-sm text-gray-600">{action}</p>
      <p className="mt-2 text-xs text-gray-400">
        Progreso estimado: +{estimated_delta} puntos en {estimated_weeks} semana
        {estimated_weeks !== 1 ? "s" : ""}
      </p>
    </div>
  );
}

function Block3Milestone({ data }: { data: MomentumProfileResponse }) {
  if (!data.milestones_unlocked || data.milestones_unlocked.length === 0) {
    return (
      <p className="text-sm text-gray-400">
        Mejorá tu score esta semana para desbloquear el primer hito.
      </p>
    );
  }

  const last = data.milestones_unlocked.at(-1);
  if (!last) return null;

  return (
    <div className="flex items-center gap-3">
      <Badge variant="success" className="text-xs">
        {last.code}
      </Badge>
      <span className="text-sm font-medium text-gray-700">{last.label}</span>
    </div>
  );
}

function Block4Value({ data }: { data: MomentumProfileResponse }) {
  const value = data.estimated_value_protected_ars ?? 0;

  return (
    <div>
      <p className="mb-1 text-sm text-gray-600">
        Desde que usás Véktor estimamos que protegiste:
      </p>
      <p className="font-bold text-vk-text-primary" style={{ fontSize: 28 }}>
        {formatARS(value)}
      </p>
      <p className="mt-1 text-xs text-gray-400">
        Estimación basada en mejoras de margen y caja.
      </p>
    </div>
  );
}

function MomentumSkeleton() {
  return (
    <div className="animate-pulse space-y-4">
      <div className="h-16 w-32 rounded-lg bg-gray-100" />
      <div className="h-28 rounded-lg bg-gray-100" />
      <div className="h-16 rounded-lg bg-gray-100" />
    </div>
  );
}

// ── Main widget ───────────────────────────────────────────────────────────────

export function MomentumWidget() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["momentum", "profile"],
    queryFn: fetchMomentumProfile,
    staleTime: 60 * 60 * 1000, // 1 hour
    retry: 1,
  });

  const isEmpty =
    !isLoading &&
    (isError ||
      data == null ||
      (data.weekly_history.length === 0 && data.best_score_ever === null));

  return (
    <div className="rounded-xl border border-gray-200 bg-white p-6 shadow-sm">
      {/* Header */}
      <p className="mb-5 text-xs font-medium uppercase tracking-widest text-gray-400">
        Tu avance con Véktor
      </p>

      {isLoading && <MomentumSkeleton />}

      {isEmpty && (
        <p className="py-6 text-center text-sm text-gray-400">
          Tu avance aparecerá después de la primera semana completa.
        </p>
      )}

      {!isLoading && !isEmpty && data && (
        <div className="grid grid-cols-2 gap-6 lg:grid-cols-4">
          {/* Bloque 1 — Score y evolución */}
          <div className="col-span-2 lg:col-span-2">
            <Block1Score data={data} />
          </div>

          {/* Bloque 2 — Meta activa */}
          <div className="col-span-2 lg:col-span-1 flex flex-col justify-center">
            <Block2Goal data={data} />
          </div>

          {/* Bloque 3 — Hito + Bloque 4 — Valor protegido */}
          <div className="col-span-2 lg:col-span-1 flex flex-col justify-between gap-4">
            <div>
              <p className="mb-2 text-xs font-medium uppercase tracking-widest text-gray-400">
                Hito reciente
              </p>
              <Block3Milestone data={data} />
            </div>
            <div className="border-t border-gray-100 pt-4">
              <Block4Value data={data} />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
