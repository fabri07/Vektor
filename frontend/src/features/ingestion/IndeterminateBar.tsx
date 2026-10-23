"use client";

/**
 * Barra de progreso REALMENTE indeterminada: un segmento que recorre el track en
 * loop, sin porcentaje. No simula progreso — solo indica "algo está en curso y no
 * sabemos cuánto falta", que es la verdad tanto durante un import como durante
 * una relectura (ninguno de los dos reporta filas procesadas).
 *
 * `label` nombra la operación para lectores de pantalla; por defecto "Importando"
 * (el primer uso, en `FileListSection`). `RereadProgress` le pasa la etapa en
 * curso, porque anunciar "Importando" durante una relectura sería falso.
 *
 * Theme-aware con tokens `vk-*`. Respeta `prefers-reduced-motion`: si el usuario
 * lo pidió, el segmento queda quieto (ocupando el ancho completo) en vez de animar.
 */
export function IndeterminateBar({ label = "Importando" }: { label?: string }) {
  return (
    <div
      className="relative h-1.5 w-full overflow-hidden rounded-full bg-vk-info-bg"
      role="progressbar"
      aria-label={label}
      aria-busy="true"
    >
      <div className="absolute h-full rounded-full bg-vk-info animate-indeterminate motion-reduce:left-0 motion-reduce:right-0 motion-reduce:animate-none" />
    </div>
  );
}
