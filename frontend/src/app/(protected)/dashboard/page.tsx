"use client";

import { useQuery } from "@tanstack/react-query";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef } from "react";
import { customersService } from "@/services/customers.service";
import { fetchCurrentInsight, fetchLatestScore } from "@/services/dashboard.service";
import { expensesService } from "@/services/expenses.service";
import { fetchMomentumProfile } from "@/services/momentum.service";
import { productsService } from "@/services/products.service";
import { salesService } from "@/services/sales.service";
import { suppliersService } from "@/services/suppliers.service";
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

function formatDateParam(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function last30DayRange(): { from_date: string; to_date: string } {
  const to = new Date();
  const from = new Date(to);
  from.setDate(to.getDate() - 30);
  return {
    from_date: formatDateParam(from),
    to_date: formatDateParam(to),
  };
}

export default function DashboardPage() {
  const calcRetries = useRef(0);
  const touchStart = useRef<number | null>(null);
  const healthRef = useRef<HTMLDivElement>(null);
  const router = useRouter();
  const searchParams = useSearchParams();
  const dateRange = last30DayRange();

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
      const delays = [5_000, 10_000, 20_000, 40_000, 60_000];
      const delay = delays[calcRetries.current] ?? false;
      calcRetries.current += 1;
      return delay;
    },
    retry: 1,
  });

  const scoreHasData =
    scoreData != null &&
    !isCalculating(scoreData) &&
    (scoreData as HealthScoreV2Response).confidence_level !== "LOW" &&
    (scoreData as HealthScoreV2Response).data_completeness_score >= 50;

  const { data: insightData, isLoading: insightLoading } = useQuery({
    queryKey: ["insights", "current"],
    queryFn: fetchCurrentInsight,
    enabled: scoreHasData,
    retry: 1,
  });

  const { data: momentumData } = useQuery({
    queryKey: ["momentum", "profile"],
    queryFn: fetchMomentumProfile,
    retry: 1,
  });

  const { data: sales = [], isLoading: salesLoading } = useQuery({
    queryKey: ["sales-all", dateRange.from_date, dateRange.to_date],
    queryFn: () => salesService.getAllEntries(dateRange),
    staleTime: 5 * 60 * 1000,
  });

  const { data: expenses = [], isLoading: expensesLoading } = useQuery({
    queryKey: ["expenses-all", dateRange.from_date, dateRange.to_date],
    queryFn: () => expensesService.getAllEntries(dateRange),
    staleTime: 5 * 60 * 1000,
  });

  const { data: products = [], isLoading: productsLoading } = useQuery({
    queryKey: ["products-all"],
    queryFn: () => productsService.getAllProducts({ is_active: true }),
    staleTime: 5 * 60 * 1000,
  });

  const { data: suppliers = [], isLoading: suppliersLoading } = useQuery({
    queryKey: ["suppliers-all"],
    queryFn: () => suppliersService.getAllSuppliers({ is_active: true }),
    staleTime: 5 * 60 * 1000,
  });

  const { data: customers = [], isLoading: customersLoading } = useQuery({
    queryKey: ["customers-all"],
    queryFn: () => customersService.getAllCustomers({ is_active: true }),
    staleTime: 5 * 60 * 1000,
  });

  useEffect(() => {
    if (searchParams.get("focus") === "health") {
      window.setTimeout(() => {
        healthRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
      }, 150);
    }
  }, [searchParams]);

  const loading = scoreLoading || insightLoading;
  const calculating = !scoreLoading && scoreData != null && isCalculating(scoreData);
  const calculatingTimeout = calculating && calcRetries.current >= 6;

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

      <div
        ref={healthRef}
        className={searchParams.get("focus") === "health" ? "rounded-2xl ring-2 ring-vk-blue/40" : ""}
      >
        <HealthScoreCard
          score={score}
          insight={insightData?.insight}
          action={insightData?.action_suggestion}
          delta={delta}
          isBestScore={isBestScore}
        />
      </div>

      <DashboardSummaryCards
        sales={sales}
        expenses={expenses}
        products={products}
        suppliers={suppliers}
        customers={customers}
        loading={
          salesLoading ||
          expensesLoading ||
          productsLoading ||
          suppliersLoading ||
          customersLoading
        }
      />

      {score.confidence_level !== "LOW" && score.data_completeness_score >= 50 && (
        <HealthAlertBanner score={score} />
      )}
    </div>
  );
}
