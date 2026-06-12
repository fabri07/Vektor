"use client";

import { useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useRef } from "react";
import { expensesService } from "@/services/expenses.service";
import { productsService } from "@/services/products.service";
import { salesService } from "@/services/sales.service";
import { fetchHealthScoreHistory } from "@/services/dashboard.service";
import { DashboardAnalysisScreen } from "@/features/dashboard/DashboardAnalysisScreen";
import { DashboardLaunchpadNav } from "@/features/dashboard/DashboardLaunchpadNav";

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

export default function DashboardAnalysisPage() {
  const touchStart = useRef<number | null>(null);
  const router = useRouter();
  const dateRange = last30DayRange();

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
      <DashboardAnalysisScreen
        sales={sales}
        expenses={expenses}
        products={products}
        scoreHistory={scoreHistory}
        loading={salesLoading || expensesLoading || productsLoading}
      />
    </div>
  );
}
