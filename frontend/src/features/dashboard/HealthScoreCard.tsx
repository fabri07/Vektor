"use client";

import { Badge } from "@/components/ui/Badge";
import type { HealthScoreV2Response } from "@/types/api";

interface Props {
  score: HealthScoreV2Response;
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

function formatDate(iso: string): string {
  return new Date(iso).toLocaleString("es-AR", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function HealthScoreCard({ score }: Props) {
  const confidenceKey = score.confidence_level?.toUpperCase() ?? "";
  const badgeVariant: BadgeVariant = CONFIDENCE_VARIANT[confidenceKey] ?? "default";
  const confidenceLabel = CONFIDENCE_LABEL[confidenceKey] ?? score.confidence_level;
  const completeness = Math.round(score.data_completeness_score);

  return (
    <div className="col-span-2 rounded-xl border border-gray-200 bg-white p-6 shadow-sm">
      <div className="mb-4 flex items-start justify-between">
        <div>
          <p className="text-xs font-medium uppercase tracking-widest text-gray-400">
            Health Score
          </p>
          <div className="mt-1 flex items-end gap-3">
            <span
              className="font-bold leading-none text-[#1A1A2E]"
              style={{ fontSize: "72px" }}
            >
              {score.score_total}
            </span>
            <span className="mb-2 text-2xl font-light text-gray-400">/ 100</span>
          </div>
        </div>
        <Badge variant={badgeVariant} className="mt-1 text-xs">
          Confianza {confidenceLabel}
        </Badge>
      </div>

      {/* Tendencia */}
      <TrendIndicator score={score} />

      {/* Barra de completitud */}
      <div className="mt-5">
        <div className="mb-1 flex items-center justify-between">
          <span className="text-xs text-gray-500">Completitud de datos</span>
          <span className="text-xs font-semibold text-gray-700">{completeness}%</span>
        </div>
        <div className="h-1.5 w-full overflow-hidden rounded-full bg-gray-100">
          <div
            className="h-full rounded-full bg-[#1A1A2E] transition-all"
            style={{ width: `${completeness}%` }}
          />
        </div>
      </div>

      <p className="mt-4 text-xs text-gray-400">
        Actualizado {formatDate(score.created_at)}
      </p>
    </div>
  );
}

function TrendIndicator({ score }: { score: HealthScoreV2Response }) {
  // Trend info is not available in a single snapshot; show level instead.
  const levelLabel: Record<string, string> = {
    excellent: "Excelente",
    good: "Bueno",
    warning: "Atención",
    critical: "Crítico",
  };
  const levelColor: Record<string, string> = {
    excellent: "text-emerald-600",
    good: "text-emerald-500",
    warning: "text-amber-500",
    critical: "text-red-600",
  };
  const key = score.level?.toLowerCase() ?? "";
  return (
    <div className={`text-sm font-medium ${levelColor[key] ?? "text-gray-500"}`}>
      {levelLabel[key] ?? score.level}
    </div>
  );
}
