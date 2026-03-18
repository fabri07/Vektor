"use client";

import { Badge } from "@/components/ui/Badge";
import type { HealthScoreV2Response } from "@/types/api";

interface Props {
  score: HealthScoreV2Response;
  delta?: number | null;
  isBestScore?: boolean;
}

type BadgeVariant = "success" | "warning" | "danger" | "default";

const CONFIDENCE_LABEL: Record<string, string> = {
  HIGH: "ALTA",
  MEDIUM: "MEDIA",
  LOW: "BAJA",
};

const CONFIDENCE_VARIANT: Record<string, BadgeVariant> = {
  HIGH: "success",
  MEDIUM: "warning",
  LOW: "default",
};

function formatRelativeTime(iso: string): string {
  const diffMs = Date.now() - new Date(iso).getTime();
  const diffMin = Math.floor(diffMs / 60_000);
  if (diffMin < 1) return "Actualizado hace unos segundos";
  if (diffMin < 60) return `Actualizado hace ${diffMin} min`;
  const diffHrs = Math.floor(diffMin / 60);
  if (diffHrs < 24) return `Actualizado hace ${diffHrs} ${diffHrs === 1 ? "hora" : "horas"}`;
  const diffDays = Math.floor(diffHrs / 24);
  return `Actualizado hace ${diffDays} ${diffDays === 1 ? "día" : "días"}`;
}

function DeltaIndicator({ delta }: { delta: number | null | undefined }) {
  if (delta == null) return null;
  if (delta > 0) {
    return (
      <span className="flex items-center gap-1 text-base font-medium text-vk-success">
        ↑ +{delta} vs semana pasada
      </span>
    );
  }
  if (delta < 0) {
    return (
      <span className="flex items-center gap-1 text-base font-medium text-vk-danger">
        ↓ {delta} vs semana pasada
      </span>
    );
  }
  return (
    <span className="text-base font-medium text-vk-text-muted">
      → Sin cambios vs semana pasada
    </span>
  );
}

export function HealthScoreCard({ score, delta, isBestScore }: Props) {
  const confidenceKey = score.confidence_level?.toUpperCase() ?? "";
  const badgeVariant: BadgeVariant = CONFIDENCE_VARIANT[confidenceKey] ?? "default";
  const confidenceLabel = CONFIDENCE_LABEL[confidenceKey] ?? score.confidence_level;
  const completeness = Math.round(score.data_completeness_score);

  return (
    <div className="rounded-lg border border-vk-border-w border-l-4 border-l-vk-blue bg-vk-surface-w p-6 shadow-vk-sm">
      {/* Header row */}
      <div className="mb-4 flex items-start justify-between gap-4">
        <p className="text-xs font-medium uppercase tracking-widest text-vk-text-muted">
          Health Score
        </p>
        <div className="flex items-center gap-2 flex-wrap justify-end">
          {isBestScore && (
            <Badge variant="warning">⭐ Tu mejor semana</Badge>
          )}
          <Badge variant={badgeVariant}>Confianza {confidenceLabel}</Badge>
        </div>
      </div>

      {/* Score + delta */}
      <div className="flex items-end gap-4">
        <div className="flex items-end gap-2">
          <span
            className="font-bold leading-none text-vk-navy"
            style={{ fontSize: "var(--vk-text-metric)" }}
          >
            {score.score_total}
          </span>
          <span className="mb-1 text-2xl font-light text-vk-text-muted">/ 100</span>
        </div>
        <div className="mb-1">
          <DeltaIndicator delta={delta} />
        </div>
      </div>

      {/* Completeness bar */}
      <div className="mt-5">
        <div className="mb-1.5 flex items-center justify-between">
          <span className="text-xs text-vk-text-muted">Completitud de datos</span>
          <span className="text-xs font-semibold text-vk-text-secondary">{completeness}%</span>
        </div>
        <div className="h-1 w-full overflow-hidden rounded-full bg-vk-border-w">
          <div
            className="h-full rounded-full bg-vk-blue transition-all"
            style={{ width: `${completeness}%` }}
          />
        </div>
      </div>

      {/* Timestamp */}
      <p className="mt-4 text-xs text-vk-text-muted">
        {formatRelativeTime(score.created_at)}
      </p>
    </div>
  );
}
