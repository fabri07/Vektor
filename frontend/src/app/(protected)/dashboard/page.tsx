"use client";

import { useQuery } from "@tanstack/react-query";
import { fetchLatestScore, fetchCurrentInsight } from "@/services/dashboard.service";
import { HealthScoreCard } from "@/features/dashboard/HealthScoreCard";
import { RiskCard } from "@/features/dashboard/RiskCard";
import { ActionCard } from "@/features/dashboard/ActionCard";
import { SubscoresCard } from "@/features/dashboard/SubscoresCard";
import { DashboardSkeleton } from "@/features/dashboard/DashboardSkeleton";
import { EmptyState } from "@/features/dashboard/EmptyState";
import type { HealthScoreV2Response } from "@/types/api";

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

  const {
    data: insightData,
    isLoading: insightLoading,
  } = useQuery({
    queryKey: ["insights", "current"],
    queryFn: fetchCurrentInsight,
    retry: 1,
  });

  const loading = scoreLoading || insightLoading;
  const calculating = !scoreLoading && scoreData != null && isCalculating(scoreData);
  const noScore = !scoreLoading && (scoreError || scoreData == null || calculating);

  if (loading || calculating) {
    return (
      <div className="space-y-4">
        <h1 className="text-lg font-semibold text-white">Dashboard</h1>
        <DashboardSkeleton />
      </div>
    );
  }

  if (noScore) {
    return (
      <div className="flex h-full flex-col gap-4">
        <h1 className="text-lg font-semibold text-white">Dashboard</h1>
        <EmptyState />
      </div>
    );
  }

  const score = scoreData as HealthScoreV2Response;

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold text-white">Dashboard</h1>
      <div className="grid grid-cols-4 gap-4">
        <HealthScoreCard score={score} />
        {insightData ? (
          <RiskCard insight={insightData.insight} />
        ) : (
          <NoInsightCard label="Riesgo Principal" />
        )}
        {insightData?.action_suggestion ? (
          <ActionCard action={insightData.action_suggestion} />
        ) : (
          <NoInsightCard label="Acción Sugerida" />
        )}
        <SubscoresCard score={score} />
      </div>
    </div>
  );
}

function NoInsightCard({ label }: { label: string }) {
  return (
    <div className="rounded-xl border border-gray-200 bg-white p-6 shadow-sm">
      <p className="mb-3 text-xs font-medium uppercase tracking-widest text-gray-400">
        {label}
      </p>
      <p className="text-sm text-gray-400">Sin datos todavía.</p>
    </div>
  );
}
