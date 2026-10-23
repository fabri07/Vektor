"use client";

/**
 * E6c-3 — qué ve alguien mientras su importación corre en segundo plano.
 *
 * Lo que esta pantalla tiene que resolver no es "mostrar un spinner": es que la
 * persona sepa **que puede irse**. Antes, cerrar la pestaña durante un confirm de
 * varios minutos dejaba el trabajo en un estado que nadie podía consultar; ahora
 * puede cerrarla y volver, y por eso el texto lo dice explícitamente en vez de
 * dejarla adivinando.
 *
 * Y cuando falla, lo que se muestra es el `error_detail` del backend — que ya
 * viene redactado como una acción ("dividí el archivo", "recalculá las fórmulas")
 * y no como un diagnóstico interno.
 */

import type { ImportacionResponse } from "@/services/ingestion.service";

import { textoDeEstado } from "./importacionAsincronica";

interface Props {
  importacion: ImportacionResponse;
  /** Error del CLIENTE (red), distinto del error de la importación. */
  errorDeRed?: string | null;
  onCerrar?: () => void;
}

export function ImportacionEnCursoPanel({ importacion, errorDeRed, onCerrar }: Props) {
  const { status, phase, rows_total: total, rows_done: hechas } = importacion;
  const enCurso = status === "PENDIENTE" || status === "EJECUTANDO";
  const fallo = status === "FALLADO" || status === "CANCELADO";
  // Sólo hay barra si el servidor sabe cuántas filas son: una barra inventada
  // sobre un total desconocido miente sobre cuánto falta.
  const porcentaje =
    total && total > 0 ? Math.min(100, Math.round((hechas / total) * 100)) : null;

  return (
    <div
      className="rounded-lg border border-vektor-border bg-vektor-surface p-4"
      role="status"
      aria-live="polite"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="font-medium text-white">{textoDeEstado(status, phase)}</p>
          {enCurso && (
            <p className="mt-1 text-sm text-white/60">
              Podés cerrar esta pantalla: la importación sigue y vas a poder ver el
              resultado cuando vuelvas.
            </p>
          )}
        </div>
        {!enCurso && onCerrar && (
          <button
            type="button"
            onClick={onCerrar}
            className="shrink-0 text-sm text-white/60 underline hover:text-white"
          >
            Cerrar
          </button>
        )}
      </div>

      {enCurso && porcentaje !== null && (
        <div className="mt-3">
          <div className="h-2 w-full overflow-hidden rounded bg-white/10">
            <div
              className="h-full bg-emerald-500 transition-all"
              style={{ width: `${porcentaje}%` }}
            />
          </div>
          <p className="mt-1 text-xs text-white/50">
            {hechas.toLocaleString("es-AR")} de {total?.toLocaleString("es-AR")} filas
          </p>
        </div>
      )}

      {enCurso && porcentaje === null && (
        <div className="mt-3 h-2 w-full overflow-hidden rounded bg-white/10">
          {/* Sin total conocido: indeterminada. No se inventa un porcentaje. */}
          <div className="h-full w-1/3 animate-pulse rounded bg-emerald-500/60" />
        </div>
      )}

      {errorDeRed && enCurso && (
        <p className="mt-3 text-sm text-amber-300">{errorDeRed}</p>
      )}

      {fallo && (
        <div className="mt-3 rounded border border-red-500/40 bg-red-500/10 p-3">
          <p className="text-sm text-red-200">
            {importacion.error_detail ??
              "No quedó registrado el motivo. Volvé a intentar la importación."}
          </p>
        </div>
      )}
    </div>
  );
}
