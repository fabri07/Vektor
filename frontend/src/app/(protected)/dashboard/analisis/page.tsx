"use client";

import { useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useRef } from "react";
import { expensesService } from "@/services/expenses.service";
import { productsService } from "@/services/products.service";
import { salesService } from "@/services/sales.service";
import { DashboardAnalysisScreen } from "@/features/dashboard/DashboardAnalysisScreen";
import { DashboardLaunchpadNav } from "@/features/dashboard/DashboardLaunchpadNav";

export default function DashboardAnalysisPage() {
  const touchStart = useRef<number | null>(null);
  const router = useRouter();

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
      }}
    >
      <DashboardLaunchpadNav active="analisis" />
      <DashboardAnalysisScreen
        sales={sales}
        expenses={expenses}
        products={products}
        loading={salesLoading || expensesLoading || productsLoading}
      />
    </div>
  );
}
