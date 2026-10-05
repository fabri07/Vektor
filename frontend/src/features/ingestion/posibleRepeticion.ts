/**
 * Fase 2 (ASTERIA, 2026-10-05) — el archivo coincide EN SU MAYORÍA con
 * operaciones ya importadas.
 *
 * El backend no guarda nada y responde con un código propio: 409
 * `POSIBLE_REPETICION` en el confirm sincrónico, o un intento asíncrono
 * `FALLADO` con `error_code = "posible_repeticion"`. No es un error de mapeo ni
 * un 409 de "ya se está importando": es una decisión que la persona tiene que
 * tomar a sabiendas, y se reenvía con `acepto_posible_repeticion: true`.
 */

import type { ImportacionResponse } from "@/services/ingestion.service";

export const POSIBLE_REPETICION_CODE = "POSIBLE_REPETICION";
export const ERROR_POSIBLE_REPETICION = "posible_repeticion";

/** El mensaje del backend si el confirm sincrónico se frenó por repetición. */
export function repeticionDelConfirm(error: unknown): string | null {
  const e = error as {
    response?: { status?: number; data?: { detail?: unknown } };
  } | null;
  if (e?.response?.status !== 409) return null;
  const detalle = e.response.data?.detail as { code?: string; message?: string } | undefined;
  if (detalle && typeof detalle === "object" && detalle.code === POSIBLE_REPETICION_CODE) {
    return detalle.message ?? "Este archivo parece repetir operaciones ya cargadas.";
  }
  return null;
}

/** El mensaje si la importación en segundo plano se frenó por repetición. */
export function repeticionDelIntento(imp: ImportacionResponse | null): string | null {
  if (imp?.status === "FALLADO" && imp.error_code === ERROR_POSIBLE_REPETICION) {
    return imp.error_detail ?? "Este archivo parece repetir operaciones ya cargadas.";
  }
  return null;
}
