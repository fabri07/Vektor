"use client";

import { useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import {
  healthScoreService,
  type HealthScoreLatest,
} from "@/services/health_score.service";
import { Badge } from "@/components/ui/Badge";

const LEVEL_LABELS: Record<string, string> = {
  HEALTHY: "Saludable",
  MODERATE: "Moderado",
  AT_RISK: "En riesgo",
  CRITICAL: "Crítico",
};

const LEVEL_BADGE_VARIANT: Record<string, "success" | "warning" | "danger" | "default"> = {
  HEALTHY: "success",
  MODERATE: "warning",
  AT_RISK: "danger",
  CRITICAL: "danger",
};

const MAX_POLLS = 20; // 40 segundos máx

function ScorePreview({ score }: { score: HealthScoreLatest }) {
  const levelLabel = LEVEL_LABELS[score.level] ?? score.level;
  const badgeVariant = LEVEL_BADGE_VARIANT[score.level] ?? "default";

  return (
    <div className="mt-8 w-full rounded-2xl border border-vk-border-w bg-vk-surface-w p-6 text-center shadow-vk-sm">
      <p className="text-sm font-medium text-vk-text-muted">
        Tu puntaje de salud financiera
      </p>
      <p className="mt-2 leading-none">
        <span
          className="font-bold text-vk-navy"
          style={{ fontSize: "var(--vk-text-metric)" }}
        >
          {Math.round(Number(score.score_total))}
        </span>
        <span className="text-2xl font-light text-vk-text-muted">/100</span>
      </p>
      <div className="mt-3 flex justify-center">
        <Badge variant={badgeVariant}>{levelLabel}</Badge>
      </div>
      <p className="mt-4 text-sm text-vk-text-muted">
        Redirigiendo a tu panel...
      </p>
    </div>
  );
}

export function Step4Loading() {
  const router = useRouter();
  const pollCount = useRef(0);

  const { data: score } = useQuery<HealthScoreLatest | null>({
    queryKey: ["health-score-latest-onboarding"],
    queryFn: healthScoreService.getLatest,
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
      router.replace("/chat");
    }, 2_500);
    return () => clearTimeout(t);
  }, [score, router]);

  // Fallback: redirigir después de MAX_POLLS * 2s + 2s de buffer
  useEffect(() => {
    const t = setTimeout(
      () => {
        router.replace("/chat");
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
            <div className="absolute inset-0 animate-spin rounded-full border-4 border-vk-border-w border-t-vk-navy" />
          </div>
          <h2 className="mt-6 text-xl font-semibold text-vk-text-primary">
            Perfecto. Analizando tu negocio...
          </h2>
          <p className="mt-2 text-sm text-vk-text-muted">
            Esto tarda menos de 10 segundos.
          </p>
        </>
      ) : (
        <ScorePreview score={score} />
      )}
    </div>
  );
}
