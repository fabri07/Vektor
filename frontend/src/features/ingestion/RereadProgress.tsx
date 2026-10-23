"use client";

import { useEffect, useState } from "react";

import { IndeterminateBar } from "./IndeterminateBar";

/**
 * Indicador de "en curso" para la relectura. NO reporta avance: ni el preview ni
 * el apply informan filas procesadas, así que no hay nada que medir. Hasta
 * 2026-08-26 esta barra animaba un porcentaje propio de 8% a 92% con un
 * `setInterval` — un número inventado que sugería una medición inexistente.
 * Ahora usa la misma `IndeterminateBar` honesta que el import
 * (`FileListSection`, estado IMPORTING), que dice "algo está en curso" sin
 * afirmar cuánto falta.
 *
 * Lo que sí es real y se conserva: el total de filas del archivo (conocido desde
 * el preview) y el tiempo transcurrido. Las etapas ciclan solo como pista de qué
 * hace el backend; al terminar, el padre desmonta el componente.
 */
const STAGES = [
  "Descargando archivo…",
  "Interpretando filas…",
  "Comparando con lo cargado…",
  "Calculando cambios…",
];

// Fase 10 (progreso con contexto, revisión externa 2026-08-20): "Ns"/"Xm Ys" —
// sin decimales ni unidades de más, legible de un vistazo mientras se actualiza.
function formatElapsed(seconds: number): string {
  if (seconds < 60) return `${Math.max(0, Math.floor(seconds))}s`;
  const mins = Math.floor(seconds / 60);
  const secs = Math.floor(seconds % 60);
  return `${mins}m ${secs}s`;
}

export function RereadProgress({
  label,
  totalRows,
  startedAt,
}: {
  label?: string;
  /** Fase 10: total de filas del archivo (conocido desde el preview) — solo
   * contexto informativo, no mide avance real fila a fila. */
  totalRows?: number | null;
  /** Fase 10: ISO de cuándo el run entró en curso (QUEUED/APPLYING), para el
   * cronómetro "empezado hace...". */
  startedAt?: string | null;
}) {
  const [stageIdx, setStageIdx] = useState(0);
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    // El ciclado de etapas solo aplica si NO hay un label fijo (fase 'applying').
    if (label) return;
    const stages = setInterval(() => {
      setStageIdx((i) => (i + 1) % STAGES.length);
    }, 1300);
    return () => clearInterval(stages);
  }, [label]);

  useEffect(() => {
    if (!startedAt) {
      setElapsed(0);
      return;
    }
    const startedMs = new Date(startedAt).getTime();
    const update = () => setElapsed((Date.now() - startedMs) / 1000);
    update();
    const tick = setInterval(update, 1000);
    return () => clearInterval(tick);
  }, [startedAt]);

  const stage = label ?? STAGES[stageIdx];

  const context = [
    totalRows != null ? `~${totalRows.toLocaleString("es-AR")} fila(s)` : null,
    startedAt ? `empezado hace ${formatElapsed(elapsed)}` : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <div className="flex flex-col gap-2 py-2">
      <p className="text-xs text-vektor-body">{stage}</p>
      <IndeterminateBar label={stage} />
      {context && <p className="text-[11px] text-vektor-muted">{context}</p>}
    </div>
  );
}
