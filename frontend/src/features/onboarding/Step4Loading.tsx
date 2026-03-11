"use client";

import { useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import {
  healthScoreService,
  type HealthScoreCurrent,
} from "@/services/health_score.service";

const LEVEL_LABELS: Record<string, string> = {
  HEALTHY: "Saludable",
  MODERATE: "Moderado",
  AT_RISK: "En riesgo",
  CRITICAL: "Crítico",
};

const LEVEL_COLORS: Record<string, string> = {
  HEALTHY: "text-green-600 bg-green-50 border-green-200",
  MODERATE: "text-yellow-600 bg-yellow-50 border-yellow-200",
  AT_RISK: "text-orange-600 bg-orange-50 border-orange-200",
  CRITICAL: "text-red-600 bg-red-50 border-red-200",
};

const MAX_POLLS = 20; // 40 seconds max

function ScorePreview({ score }: { score: HealthScoreCurrent }) {
  const levelLabel = LEVEL_LABELS[score.level] ?? score.level;
  const levelColor =
    LEVEL_COLORS[score.level] ?? "text-gray-600 bg-gray-50 border-gray-200";

  return (
    <div className="mt-8 rounded-2xl border border-gray-200 bg-white p-6 text-center shadow-sm">
      <p className="text-sm font-medium text-gray-500">
        Tu puntaje de salud financiera
      </p>
      <p className="mt-2 text-5xl font-bold text-gray-900">
        {Math.round(Number(score.total_score))}
        <span className="text-2xl text-gray-400">/100</span>
      </p>
      <span
        className={[
          "mt-3 inline-block rounded-full border px-3 py-1 text-xs font-semibold",
          levelColor,
        ].join(" ")}
      >
        {levelLabel}
      </span>
      <p className="mt-4 text-sm text-gray-500">
        Redirigiendo a tu panel...
      </p>
    </div>
  );
}

export function Step4Loading() {
  const router = useRouter();
  const pollCount = useRef(0);

  const { data: score } = useQuery<HealthScoreCurrent | null>({
    queryKey: ["health-score-current-onboarding"],
    queryFn: healthScoreService.getCurrent,
    refetchInterval: (query) => {
      if (query.state.data != null) return false;
      if (pollCount.current >= MAX_POLLS) return false;
      pollCount.current += 1;
      return 2_000;
    },
    retry: false,
  });

  useEffect(() => {
    if (!score) return;
    const t = setTimeout(() => {
      router.replace("/dashboard");
    }, 2_500);
    return () => clearTimeout(t);
  }, [score, router]);

  // Timeout fallback: redirect after MAX_POLLS * 2s + 2s buffer
  useEffect(() => {
    const t = setTimeout(
      () => {
        router.replace("/dashboard");
      },
      (MAX_POLLS + 1) * 2_000,
    );
    return () => clearTimeout(t);
  }, [router]);

  return (
    <div className="flex flex-col items-center py-8 text-center">
      {!score ? (
        <>
          <div className="relative flex h-20 w-20 items-center justify-center">
            <div className="absolute inset-0 animate-spin rounded-full border-4 border-gray-200 border-t-[#1A1A2E]" />
          </div>
          <h2 className="mt-6 text-xl font-semibold text-gray-900">
            Analizando tu negocio...
          </h2>
          <p className="mt-2 text-sm text-gray-500">
            Esto puede tardar unos segundos. No cerrés esta ventana.
          </p>
        </>
      ) : (
        <ScorePreview score={score} />
      )}
    </div>
  );
}
