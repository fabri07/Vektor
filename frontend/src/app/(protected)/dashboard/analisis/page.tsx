"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useRef } from "react";
import { PeriodFilter } from "@/components/ui/PeriodFilter";
import { type PeriodValue, resolvePeriod } from "@/lib/period";
import { expensesService } from "@/services/expenses.service";
import { productsService } from "@/services/products.service";
import { salesService } from "@/services/sales.service";
import { fetchHealthScoreHistory } from "@/services/dashboard.service";
import { DashboardAnalysisScreen } from "@/features/dashboard/DashboardAnalysisScreen";
import { DashboardLaunchpadNav } from "@/features/dashboard/DashboardLaunchpadNav";

export default function DashboardAnalysisPage() {
  const touchStart = useRef<number | null>(null);
  const router = useRouter();
  const [period, setPeriod] = useState<PeriodValue>({ kind: "preset", preset: "this_month" });
  const { from, to } = resolvePeriod(period);

  const { data: salesDateRange = null, isLoading: salesDateRangeLoading } = useQuery({
    queryKey: ["sales-date-range"],
    queryFn: () => salesService.getDateRange(),
    staleTime: 10 * 60 * 1000,
  });
  const { data: expensesDateRange = null, isLoading: expensesDateRangeLoading } = useQuery({
    queryKey: ["expenses-date-range"],
    queryFn: () => expensesService.getDateRange(),
    staleTime: 10 * 60 * 1000,
  });
  // Combina el rango disponible de ambas fuentes: PeriodFilter necesita saber
  // desde cuándo hay datos (para la navegación año/mes), sin importar si vinieron
  // de ventas o de gastos.
  const minDates = [salesDateRange?.min_date, expensesDateRange?.min_date].filter(
    (d): d is string => Boolean(d),
  );
  const maxDates = [salesDateRange?.max_date, expensesDateRange?.max_date].filter(
    (d): d is string => Boolean(d),
  );
  const availableRange = {
    min_date: minDates.length > 0 ? minDates.sort()[0]! : null,
    max_date: maxDates.length > 0 ? maxDates.sort().at(-1)! : null,
  };
  const hasAnyDataEver = Boolean(availableRange.min_date);
  const dateRangeLoading = salesDateRangeLoading || expensesDateRangeLoading;

  const {
    data: sales = [],
    isLoading: salesLoading,
    isError: salesError,
  } = useQuery({
    queryKey: ["sales-all", from, to],
    queryFn: () => salesService.getAllEntries({ from_date: from, to_date: to }),
    staleTime: 5 * 60 * 1000,
  });

  const {
    data: expenses = [],
    isLoading: expensesLoading,
    isError: expensesError,
  } = useQuery({
    queryKey: ["expenses-all", from, to],
    queryFn: () => expensesService.getAllEntries({ from_date: from, to_date: to }),
    staleTime: 5 * 60 * 1000,
  });

  // `getAllEntries` corta en 5000 filas (mismo tope que /sales): sin esto, un
  // período con más movimientos que ese cupo se mostraría "completo" sin avisar.
  const { data: salesTotal = 0 } = useQuery({
    queryKey: ["sales-count", from, to],
    queryFn: () => salesService.countEntries({ from_date: from, to_date: to }),
    staleTime: 5 * 60 * 1000,
  });
  const { data: expensesTotal = 0 } = useQuery({
    queryKey: ["expenses-count", from, to],
    queryFn: () => expensesService.countEntries({ from_date: from, to_date: to }),
    staleTime: 5 * 60 * 1000,
  });
  const entriesTruncated =
    (!salesLoading && salesTotal > sales.length) ||
    (!expensesLoading && expensesTotal > expenses.length);

  const { data: products = [], isLoading: productsLoading } = useQuery({
    queryKey: ["products-all"],
    queryFn: () => productsService.getAllProducts({ is_active: true }),
    staleTime: 5 * 60 * 1000,
  });

  const { data: scoreHistory = [] } = useQuery({
    queryKey: ["health-score-history"],
    queryFn: fetchHealthScoreHistory,
    staleTime: 5 * 60 * 1000,
  });

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
        if (end - start > 70) router.push("/dashboard");
        if (start - end > 70) router.push("/dashboard/balance");
      }}
    >
      <DashboardLaunchpadNav active="analisis" />
      <PeriodFilter value={period} onChange={setPeriod} availableRange={availableRange} />
      <DashboardAnalysisScreen
        sales={sales}
        expenses={expenses}
        products={products}
        scoreHistory={scoreHistory}
        loading={salesLoading || expensesLoading || productsLoading}
        from={from}
        to={to}
        entriesTruncated={entriesTruncated}
        error={salesError || expensesError}
        hasAnyDataEver={hasAnyDataEver}
        statusLoading={salesLoading || expensesLoading || dateRangeLoading}
      />
    </div>
  );
}
