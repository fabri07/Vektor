"use client";

import { useQuery } from "@tanstack/react-query";
import { fetchLatestScore, fetchCurrentInsight } from "@/services/dashboard.service";
import { fetchMomentumProfile } from "@/services/momentum.service";
import { HealthScoreCard } from "@/features/dashboard/HealthScoreCard";
import { RiskCard } from "@/features/dashboard/RiskCard";
import { ActionCard } from "@/features/dashboard/ActionCard";
import { SubscoresCard } from "@/features/dashboard/SubscoresCard";
import { DashboardSkeleton } from "@/features/dashboard/DashboardSkeleton";
import { EmptyState } from "@/features/dashboard/EmptyState";
import { MomentumWidget } from "@/features/dashboard/MomentumWidget";
import type { HealthScoreV2Response } from "@/types/api";

// Clase base para hover lift en cards del dashboard
const CARD_HOVER =
  "transition-[transform,box-shadow] duration-200 hover:-translate-y-0.5 hover:shadow-vk-md";

function isCalculating(data: unknown): boolean {
  return (
    typeof data === "object" &&
    data !== null &&
    "status" in data &&
    (data as { status: string }).status === "CALCULATING"
  );
}

export default function DashboardPage() {
  const {
    data: scoreData,
    isLoading: scoreLoading,
    isError: scoreError,
  } = useQuery({
    queryKey: ["health-scores", "latest"],
    queryFn: fetchLatestScore,
    refetchInterval: (query) =>
      isCalculating(query.state.data) ? 30_000 : false,
    retry: 1,
  });

  const { data: insightData, isLoading: insightLoading } = useQuery({
    queryKey: ["insights", "current"],
    queryFn: fetchCurrentInsight,
    retry: 1,
  });

  // Mismo query key que MomentumWidget — React Query usa caché compartida
  const { data: momentumData } = useQuery({
    queryKey: ["momentum", "profile"],
    queryFn: fetchMomentumProfile,
    retry: 1,
  });

  const loading = scoreLoading || insightLoading;
  const calculating = !scoreLoading && scoreData != null && isCalculating(scoreData);

  if (loading || calculating) {
    return (
      <div className="space-y-4">
        <h1 className="text-lg font-semibold text-vk-text-primary">Dashboard</h1>
        <DashboardSkeleton />
      </div>
    );
  }

  const noScore = scoreError || scoreData == null;

  if (noScore) {
    return (
      <div className="flex h-full flex-col gap-4">
        <h1 className="text-lg font-semibold text-vk-text-primary">Dashboard</h1>
        <EmptyState />
      </div>
    );
  }

  const score = scoreData as HealthScoreV2Response;

  // Datos de momentum para el hero card
  const lastWeek = momentumData?.weekly_history?.at(-1);
  const delta = lastWeek?.delta ?? null;
  const isBestScore =
    momentumData?.best_score_ever != null &&
    score.score_total >= momentumData.best_score_ever;

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold text-vk-text-primary">Dashboard</h1>

      {/* Hero — ancho completo */}
      <div className={CARD_HOVER}>
        <HealthScoreCard score={score} delta={delta} isBestScore={isBestScore} />
      </div>

      {/* Grid 2 columnas: Risk + Action en primera fila, Subscores full en segunda */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <div className={CARD_HOVER}>
          {insightData ? (
            <RiskCard insight={insightData.insight} />
          ) : (
            <NoInsightCard label="Riesgo Principal" />
          )}
        </div>

        <div className={CARD_HOVER}>
          {insightData?.action_suggestion ? (
            <ActionCard action={insightData.action_suggestion} />
          ) : (
            <NoInsightCard label="Acción Sugerida" />
          )}
        </div>

        {/* Subscores — ancho completo en la segunda fila */}
        <div className={`md:col-span-2 ${CARD_HOVER}`}>
          <SubscoresCard score={score} />
        </div>
      </div>

      {/* Momentum widget */}
      <div className={CARD_HOVER}>
        <MomentumWidget />
      </div>
    </div>
  );
}

function NoInsightCard({ label }: { label: string }) {
  return (
    <div className="rounded-lg border border-vk-border-w bg-vk-surface-w p-6 shadow-vk-sm">
      <p className="mb-3 text-xs font-medium uppercase tracking-widest text-vk-text-muted">
        {label}
      </p>
      <p className="text-sm text-vk-text-muted">Sin datos todavía.</p>
    </div>
  );
}
