"use client";

import { useQuery } from "@tanstack/react-query";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef } from "react";
import { customersService } from "@/services/customers.service";
import {
  esScoreReal,
  fetchCurrentInsight,
  fetchLatestScore,
} from "@/services/dashboard.service";
import { clasificarLatestScore } from "@/services/health_score.service";
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

// Sólo "¿hay que seguir esperando?". El resto de los estados se resuelven con
// `esScoreReal`. Esta función tenía antes una copia propia de la clasificación
// que sólo reconocía "CALCULATING", y por eso un `NO_DATA` se colaba entero
// hasta el cast y el dashboard se armaba alrededor de un objeto sin
// `score_total`.
//
// El `data != null` importa: `refetchInterval` corre antes del primer fetch,
// con `data` en `undefined`, y ahí no hay nada que clasificar todavía.
function isCalculating(data: unknown): boolean {
  return data != null && clasificarLatestScore(data) === "calculating";
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

  // Sin cast: `esScoreReal` es un type predicate, así que adentro del `&&` el
  // compilador ya sabe que `scoreData` es un HealthScoreV2Response. Con el
  // `as` puesto, agregar un cuarto estado en el backend no habría dado ni un
  // error de compilación.
  const scoreHasData =
    esScoreReal(scoreData) &&
    scoreData.confidence_level !== "LOW" &&
    scoreData.data_completeness_score >= 50;

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

  // `!esScoreReal` cubre `NO_DATA` y el `CALCULATING` que agotó los reintentos:
  // cualquier payload que no sea un score termina en el empty state. Antes
  // `NO_DATA` seguía de largo y el dashboard se armaba alrededor de un objeto
  // sin `score_total`.
  if (scoreError || calculatingTimeout || !esScoreReal(scoreData)) {
    return (
      <div className="space-y-5">
        <DashboardLaunchpadNav active="dashboard" />
        <EmptyState />
      </div>
    );
  }

  // Ya estrechado por el `esScoreReal` del early return: sin cast.
  const score = scoreData;
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
