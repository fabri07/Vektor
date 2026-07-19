"use client";

/**
 * Barra de progreso REALMENTE indeterminada: un segmento que recorre el track en
 * loop, sin porcentaje. A diferencia de `RereadProgress` (que avanza asintótico a
 * ~92% con un % inventado), esta no simula progreso — solo indica "algo está en
 * curso y no sabemos cuánto falta", que es la verdad durante un import.
 *
 * Theme-aware con tokens `vk-*`. Respeta `prefers-reduced-motion`: si el usuario
 * lo pidió, el segmento queda quieto (ocupando el ancho completo) en vez de animar.
 */
export function IndeterminateBar() {
  return (
    <div
      className="relative h-1.5 w-full overflow-hidden rounded-full bg-vk-info-bg"
      role="progressbar"
      aria-label="Importando"
      aria-busy="true"
    >
      <div className="absolute h-full rounded-full bg-vk-info animate-indeterminate motion-reduce:left-0 motion-reduce:right-0 motion-reduce:animate-none" />
    </div>
  );
}
