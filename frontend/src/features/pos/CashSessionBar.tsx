"use client";

import { formatCents } from "@/lib/pos/cart";
import type { TurnoLocal } from "@/stores/posCashSessionStore";

interface CashSessionBarProps {
  turno: TurnoLocal;
  onCerrar: () => void;
  /** Sólo OWNER/ADMIN: abre el panel de turnos de todas las cajas. */
  onVerTurnos?: () => void;
}

/**
 * El turno abierto, a la vista (B8).
 *
 * No muestra el efectivo esperado: a un cajero, verlo mientras el turno está
 * abierto le arruina el arqueo ciego. Sí avisa dos cosas que cambian lo que el
 * cajero tiene que hacer: que la apertura todavía no llegó al servidor, y que
 * el servidor tiene registrado OTRO fondo (otra apertura del mismo turno llegó
 * antes).
 */
export function CashSessionBar({ turno, onCerrar, onVerTurnos }: CashSessionBarProps) {
  const hora = new Date(turno.openedAt).toLocaleTimeString("es-AR", {
    hour: "2-digit",
    minute: "2-digit",
  });
  const fondoDistinto =
    turno.aperturaConfirmada && turno.openingFloatCents !== turno.declaredFloatCents;
  return (
    <div
      className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-pos-border bg-pos-surface px-4 py-1 text-sm"
      data-testid="turno-bar"
    >
      <span className="text-pos-text">
        Turno abierto {hora} · fondo {formatCents(turno.openingFloatCents)}
      </span>
      {!turno.aperturaConfirmada ? (
        <span className="text-pos-muted">· apertura sin sincronizar</span>
      ) : null}
      {fondoDistinto ? (
        <span className="text-vk-warning" role="status">
          · El fondo registrado es {formatCents(turno.openingFloatCents)} (declaraste{" "}
          {formatCents(turno.declaredFloatCents)})
        </span>
      ) : null}
      <span className="flex-1" />
      {onVerTurnos ? (
        <button type="button" className="text-pos-link underline" onClick={onVerTurnos}>
          Turnos
        </button>
      ) : null}
      <button type="button" className="font-semibold text-pos-link underline" onClick={onCerrar}>
        Cerrar turno
      </button>
    </div>
  );
}
