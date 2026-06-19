"use client";

import { useEffect, useState } from "react";

/**
 * Barra de progreso con etapas para la relectura. Como el preview/apply es una
 * sola llamada HTTP (sin streaming), la barra es animada: avanza suavemente
 * hacia ~92% mientras la operación está en curso y cicla las etiquetas de etapa.
 * Al terminar, el padre desmonta el componente (la barra no "miente" llegando a
 * 100% sola).
 */
const STAGES = [
  "Descargando archivo…",
  "Interpretando filas…",
  "Comparando con lo cargado…",
  "Calculando cambios…",
];

export function RereadProgress({ label }: { label?: string }) {
  const [pct, setPct] = useState(8);
  const [stageIdx, setStageIdx] = useState(0);

  useEffect(() => {
    // Avance asintótico hacia 92%: rápido al principio, lento al final.
    const tick = setInterval(() => {
      setPct((p) => Math.min(92, p + Math.max(1, (92 - p) * 0.12)));
    }, 220);
    // El ciclado de etapas solo aplica si NO hay un label fijo (fase 'applying').
    const stages = label
      ? null
      : setInterval(() => {
          setStageIdx((i) => (i + 1) % STAGES.length);
        }, 1300);
    return () => {
      clearInterval(tick);
      if (stages) clearInterval(stages);
    };
  }, [label]);

  return (
    <div className="flex flex-col gap-2 py-2">
      <div className="flex items-center justify-between text-xs">
        <span className="text-vektor-body">{label ?? STAGES[stageIdx]}</span>
        <span className="tabular-nums text-vektor-muted">{Math.round(pct)}%</span>
      </div>
      <div className="h-2 w-full overflow-hidden rounded-full bg-vektor-border">
        <div
          className="h-full rounded-full bg-vk-blue transition-[width] duration-200 ease-out"
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}
