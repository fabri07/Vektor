"use client";

import { useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useRef } from "react";
import { fetchCurrentInsight, fetchLatestScore } from "@/services/dashboard.service";
import { expensesService } from "@/services/expenses.service";
import { fetchMomentumProfile } from "@/services/momentum.service";
import { productsService } from "@/services/products.service";
import { salesService } from "@/services/sales.service";
import { DashboardLaunchpadNav } from "@/features/dashboard/DashboardLaunchpadNav";
import { DashboardSkeleton } from "@/features/dashboard/DashboardSkeleton";
import { EmptyState } from "@/features/dashboard/EmptyState";
import { HealthScoreCard } from "@/features/dashboard/HealthScoreCard";
import { DashboardSummaryCards } from "@/features/dashboard/DashboardSummaryCards";
import { HealthAlertBanner } from "@/components/dashboard/HealthAlertBanner";
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
  const calcRetries = useRef(0);
  const touchStart = useRef<number | null>(null);
  const router = useRouter();

  const {
    data: scoreData,
    isLoading: scoreLoading,
    isError: scoreError,
  } = useQuery({
    queryKey: ["health-scores", "latest"],
    queryFn: fetchLatestScore,
    refetchInterval: (query) => {
      if (!isCalculating(query.state.data)) {
        calcRetries.current = 0;
        return false;
      }
      calcRetries.current += 1;
      return calcRetries.current <= 1 ? 15_000 : false;
    },
    retry: 1,
  });

  const { data: insightData, isLoading: insightLoading } = useQuery({
    queryKey: ["insights", "current"],
    queryFn: fetchCurrentInsight,
    retry: 1,
  });

  const { data: momentumData } = useQuery({
    queryKey: ["momentum", "profile"],
    queryFn: fetchMomentumProfile,
    retry: 1,
  });

  const { data: sales = [], isLoading: salesLoading } = useQuery({
    queryKey: ["sales-all"],
    queryFn: () => salesService.getAllEntries(),
    staleTime: 5 * 60 * 1000,
  });

  const { data: expenses = [], isLoading: expensesLoading } = useQuery({
    queryKey: ["expenses-all"],
    queryFn: () => expensesService.getAllEntries(),
    staleTime: 5 * 60 * 1000,
  });

  const { data: products = [], isLoading: productsLoading } = useQuery({
    queryKey: ["products-all"],
    queryFn: () => productsService.getAllProducts({ is_active: true }),
    staleTime: 5 * 60 * 1000,
  });

  const loading = scoreLoading || insightLoading;
  const calculating = !scoreLoading && scoreData != null && isCalculating(scoreData);
  const calculatingTimeout = calculating && calcRetries.current > 1;

  if (loading || (calculating && !calculatingTimeout)) {
    return (
      <div className="space-y-5">
        <DashboardLaunchpadNav active="dashboard" />
        <DashboardSkeleton />
      </div>
    );
  }

  const noScore = scoreError || scoreData == null || calculatingTimeout;

  if (noScore) {
    return (
      <div className="space-y-5">
        <DashboardLaunchpadNav active="dashboard" />
        <EmptyState />
      </div>
    );
  }

  const score = scoreData as HealthScoreV2Response;
  const lastWeek = momentumData?.weekly_history?.at(-1);
  const delta = lastWeek?.delta ?? null;
  const isBestScore =
    momentumData?.best_score_ever != null &&
    score.score_total >= momentumData.best_score_ever;

  return (
    <div
      className="space-y-5 pb-24 sm:pb-8"
      onTouchStart={(event) => {
        touchStart.current = event.changedTouches[0]?.clientX ?? null;
      }}
      onTouchEnd={(event) => {
        const start = touchStart.current;
        const end = event.changedTouches[0]?.clientX ?? null;
        if (start == null || end == null) return;
        if (start - end > 70) router.push("/dashboard/analisis");
      }}
    >
      <DashboardLaunchpadNav active="dashboard" />

      <HealthScoreCard
        score={score}
        insight={insightData?.insight}
        action={insightData?.action_suggestion}
        delta={delta}
        isBestScore={isBestScore}
      />

      <DashboardSummaryCards
        sales={sales}
        expenses={expenses}
        products={products}
        loading={salesLoading || expensesLoading || productsLoading}
      />

      <HealthAlertBanner score={score} />
    </div>
  );
}
