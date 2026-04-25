"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, ArrowRight, Sparkles, TrendingUp } from "lucide-react";
import { Tooltip } from "@/components/ui/Tooltip";
import type {
  ActionSuggestionResponse,
  HealthScoreV2Response,
  InsightResponse,
} from "@/types/api";

interface Props {
  score: HealthScoreV2Response;
  insight?: InsightResponse | null;
  action?: ActionSuggestionResponse | null;
  delta?: number | null;
  isBestScore?: boolean;
}

function gaugeTone(score: number) {
  if (score <= 40) return "var(--vektor-red)";
  if (score <= 65) return "var(--vektor-amber)";
  if (score <= 85) return "var(--vektor-blue)";
  return "var(--vektor-teal)";
}

function describeRisk(riskCode: string, fallback?: string | null): string {
  const key = riskCode.toLowerCase();
  if (fallback) return fallback;
  if (key.includes("cash") || key.includes("caja")) {
    return "La caja disponible es tu principal punto de tension: si los egresos siguen acelerando, te quedas con menos margen para operar tranquilo.";
  }
  if (key.includes("margin") || key.includes("margen")) {
    return "Tu rentabilidad se esta debilitando en categorias clave y eso te obliga a vender mas para ganar lo mismo.";
  }
  if (key.includes("stock")) {
    return "La distribucion del stock esta desbalanceada: hay riesgo de perder ventas en algunos productos y de inmovilizar plata en otros.";
  }
  if (key.includes("supplier") || key.includes("proveedor")) {
    return "La dependencia de pocos proveedores aumenta tu exposicion a faltantes, subas de precio y demoras de reposicion.";
  }
  return "Hoy hay senales mixtas entre liquidez, margen y operacion. Conviene atacar primero el factor con mayor impacto para recuperar estabilidad.";
}

function Gauge({ score }: { score: number }) {
  const radius = 86;
  const circumference = Math.PI * radius;
  const dashArray = circumference;
  const [progress, setProgress] = useState(0);
  const stroke = gaugeTone(score);

  useEffect(() => {
    const timeout = window.setTimeout(() => setProgress(score), 60);
    return () => window.clearTimeout(timeout);
  }, [score]);

  const dashOffset = dashArray - (progress / 100) * dashArray;

  return (
    <div className="relative flex flex-col items-center">
      <svg viewBox="0 0 220 150" className="h-[180px] w-full max-w-[280px] overflow-visible">
        <path
          d="M 24 126 A 86 86 0 0 1 196 126"
          fill="none"
          stroke="rgba(240,108,121,0.85)"
          strokeWidth="14"
          strokeLinecap="round"
          strokeDasharray={`${circumference * 0.4} ${circumference}`}
        />
        <path
          d="M 24 126 A 86 86 0 0 1 196 126"
          fill="none"
          stroke="rgba(241,182,72,0.85)"
          strokeWidth="14"
          strokeLinecap="round"
          strokeDasharray={`${circumference * 0.25} ${circumference}`}
          strokeDashoffset={-circumference * 0.4}
        />
        <path
          d="M 24 126 A 86 86 0 0 1 196 126"
          fill="none"
          stroke="rgba(58,134,255,0.85)"
          strokeWidth="14"
          strokeLinecap="round"
          strokeDasharray={`${circumference * 0.2} ${circumference}`}
          strokeDashoffset={-circumference * 0.65}
        />
        <path
          d="M 24 126 A 86 86 0 0 1 196 126"
          fill="none"
          stroke="rgba(39,199,184,0.9)"
          strokeWidth="14"
          strokeLinecap="round"
          strokeDasharray={`${circumference * 0.15} ${circumference}`}
          strokeDashoffset={-circumference * 0.85}
        />
        <path
          d="M 24 126 A 86 86 0 0 1 196 126"
          fill="none"
          stroke="rgba(255,255,255,0.08)"
          strokeWidth="10"
          strokeLinecap="round"
        />
        <path
          d="M 24 126 A 86 86 0 0 1 196 126"
          fill="none"
          stroke={stroke}
          strokeWidth="10"
          strokeLinecap="round"
          strokeDasharray={dashArray}
          strokeDashoffset={dashOffset}
          style={{ transition: "stroke-dashoffset 1.2s ease, stroke 1.2s ease" }}
        />
      </svg>
      <div className="absolute bottom-1 flex flex-col items-center">
        <div className="font-display text-[48px] font-bold leading-none text-vektor-white">
          {Math.round(score)}
        </div>
        <div className="mt-2 text-sm font-medium text-vektor-body">
          Salud del negocio
        </div>
      </div>
    </div>
  );
}

export function HealthScoreCard({
  score,
  insight,
  action,
  delta,
  isBestScore,
}: Props) {
  const scoreValue = Math.round(Number(score.score_total));
  const tone = gaugeTone(scoreValue);
  const riskDescription = useMemo(
    () => describeRisk(score.primary_risk_code, insight?.description),
    [score.primary_risk_code, insight?.description],
  );

  const actionText =
    action?.description ??
    "Tu proximo mejor movimiento es reforzar la caja operativa y revisar los frentes que mas pesan sobre la salud general del negocio.";

  const deltaText =
    delta == null ? "Sin comparativa semanal disponible" : delta > 0
      ? `+${delta} vs semana pasada`
      : `${delta} vs semana pasada`;

  return (
    <section className="vektor-card overflow-hidden p-6 md:p-7">
      <div className="grid gap-6 lg:grid-cols-[0.95fr_1.05fr]">
        <div className="rounded-[28px] border border-vektor-border bg-[radial-gradient(circle_at_top,rgba(58,134,255,0.18),transparent_34%),linear-gradient(180deg,rgba(255,255,255,0.03),rgba(255,255,255,0.01))] p-5">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <Tooltip content="Una medida sintetica de 0 a 100 que combina caja, margen, stock y proveedores para mostrar la salud general del negocio.">
              <p className="text-xs font-semibold uppercase tracking-[0.18em] text-vektor-muted">
                Health Score
              </p>
            </Tooltip>
            <div className="flex flex-wrap items-center gap-2">
              {isBestScore ? (
                <span className="inline-flex items-center gap-1 rounded-full border border-vektor-border bg-vektor-surface px-3 py-1 text-xs font-medium text-vektor-body">
                  <Sparkles className="h-3.5 w-3.5 text-vektor-amber" />
                  Tu mejor semana
                </span>
              ) : null}
              <span className="inline-flex items-center gap-1 rounded-full border border-vektor-border bg-vektor-surface px-3 py-1 text-xs font-medium text-vektor-body">
                <TrendingUp className="h-3.5 w-3.5" style={{ color: tone }} />
                {deltaText}
              </span>
            </div>
          </div>

          <div className="mt-4">
            <Gauge score={scoreValue} />
          </div>
        </div>

        <div className="flex flex-col gap-4">
          <div
            className="rounded-2xl border border-vektor-border bg-vektor-surface p-5"
            style={{ borderLeftColor: tone, borderLeftWidth: 4 }}
          >
            <Tooltip content="La acción más concreta para mover hoy la aguja del negocio sin perder tiempo en diagnósticos técnicos.">
              <p className="text-base font-semibold text-vektor-white">
                Qué hacer ahora
              </p>
            </Tooltip>
            <p className="mt-3 max-w-2xl text-sm leading-7 text-vektor-body">
              {actionText}
            </p>
            <Link
              href="/dashboard/analisis"
              className="mt-4 inline-flex items-center gap-2 text-sm font-medium text-vektor-white"
            >
              Ver detalle
              <ArrowRight className="h-4 w-4" />
            </Link>
          </div>

          <div className="rounded-xl border border-vektor-red bg-red-950/70 p-4">
            <div className="flex items-start gap-3">
              <AlertTriangle className="mt-0.5 h-6 w-6 shrink-0 text-vektor-red" />
              <div>
                <Tooltip content="Este es el factor que mas amenaza la salud de tu negocio en este momento.">
                  <p className="text-sm font-semibold text-vektor-red">
                    Riesgo principal
                  </p>
                </Tooltip>
                <p className="mt-2 text-sm leading-6 text-vektor-body">
                  {riskDescription}
                </p>
              </div>
            </div>
          </div>

          <div className="grid gap-3 sm:grid-cols-3">
            {[
              { label: "Caja", value: score.score_cash },
              { label: "Margen", value: score.score_margin },
              { label: "Stock", value: score.score_stock },
            ].map((item) => (
              <div
                key={item.label}
                className="rounded-2xl border border-vektor-border bg-vektor-night/50 p-4"
              >
                <Tooltip content={`${item.label}: puntaje parcial dentro del Health Score.`}>
                  <p className="text-xs font-semibold uppercase tracking-[0.16em] text-vektor-muted">
                    {item.label}
                  </p>
                </Tooltip>
                <p className="mt-2 text-2xl font-semibold text-vektor-white">
                  {Math.round(item.value)}
                </p>
              </div>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}
