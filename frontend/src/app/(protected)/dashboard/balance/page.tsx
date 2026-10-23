"use client";

import { useRouter } from "next/navigation";
import { useRef } from "react";
import { DashboardLaunchpadNav } from "@/features/dashboard/DashboardLaunchpadNav";
import { EconomicSummaryScreen } from "@/features/economic-summary/EconomicSummaryScreen";

export default function DashboardBalancePage() {
  const router = useRouter();
  const touchStart = useRef<number | null>(null);

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
        // Swipe a la derecha → pestaña anterior (Análisis).
        if (end - start > 70) router.push("/dashboard/analisis");
      }}
    >
      <DashboardLaunchpadNav active="balance" />
      <EconomicSummaryScreen />
    </div>
  );
}
