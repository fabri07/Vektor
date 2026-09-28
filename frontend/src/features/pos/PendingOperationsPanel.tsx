"use client";

/**
 * Las ventas cobradas que todavía no están en el servidor.
 *
 * Existe porque un estado terminal sin salida es una trampa: si una venta
 * queda "requiere revisión" y no hay dónde verla ni qué hacer con ella, el
 * cajero sólo ve un número que no baja. Acá se ve el motivo, se puede
 * reintentar, y se puede exportar a un archivo como último recurso.
 *
 * El botón de descartar existe y pide confirmación explícita: es el ÚNICO
 * borrado de la cola. Nada automático borra una venta cobrada.
 */

import { useState } from "react";

import { Button } from "@/components/ui/Button";
import type { OperacionGuardada } from "@/lib/offline/posDb";
import { actualizarOperacion, borrarOperacion } from "@/lib/offline/posDb";
import { formatCents, toCents } from "@/lib/pos/cart";

const ROTULO: Record<OperacionGuardada["estado"], string> = {
  PENDIENTE: "Pendiente de sincronizar",
  SINCRONIZADA: "Sincronizada",
  REQUIERE_AUTENTICACION: "Hay que volver a entrar",
  REQUIERE_REVISION: "Requiere revisión",
};

const AYUDA: Record<OperacionGuardada["estado"], string> = {
  PENDIENTE: "Se reintenta sola cuando vuelve la conexión.",
  SINCRONIZADA: "Ya está registrada.",
  REQUIERE_AUTENTICACION:
    "La sesión venció o esta PC dejó de estar habilitada como caja. Volvé a entrar.",
  REQUIERE_REVISION: "El servidor la rechazó. Miralo con el encargado antes de reintentar.",
};

function cuando(iso: string): string {
  return new Date(iso).toLocaleString("es-AR", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

interface Props {
  pendientes: OperacionGuardada[];
  sincronizando: boolean;
  persistencia: boolean | null;
  onFlush: () => void;
  onCambio: () => void;
  onImprimir: (operacion: OperacionGuardada) => void;
}

export function PendingOperationsPanel({
  pendientes,
  sincronizando,
  persistencia,
  onFlush,
  onCambio,
  onImprimir,
}: Props) {
  const [abierto, setAbierto] = useState(false);
  const [descartando, setDescartando] = useState<string | null>(null);

  if (pendientes.length === 0) return null;

  async function reintentar(op: OperacionGuardada): Promise<void> {
    await actualizarOperacion(op.clientOperationId, {
      estado: "PENDIENTE",
      proximoIntentoEn: 0,
    });
    onCambio();
    onFlush();
  }

  async function descartar(op: OperacionGuardada): Promise<void> {
    await borrarOperacion(op.clientOperationId);
    setDescartando(null);
    onCambio();
  }

  function exportar(): void {
    // Último recurso: si el navegador evacúa los datos o la PC se rompe, esto
    // es lo único que queda para reconstruir a mano lo que se cobró.
    const blob = new Blob([JSON.stringify(pendientes, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `ventas-pendientes-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="border-t border-pos-border pt-3 text-sm">
      <button
        type="button"
        className="text-pos-text-2 hover:text-pos-text"
        onClick={() => setAbierto((v) => !v)}
      >
        {abierto ? "▾" : "▸"} Ventas sin sincronizar ({pendientes.length})
        {sincronizando ? " · sincronizando…" : ""}
      </button>
      {abierto && (
        <div className="mt-2 space-y-2">
          {persistencia === false && (
            <p className="text-xs text-pos-danger">
              El navegador no garantiza conservar estos datos. Exportalos si vas a apagar
              la PC antes de que vuelva la conexión.
            </p>
          )}
          <ul className="space-y-2">
            {pendientes.map((op) => (
              <li
                key={op.clientOperationId}
                className="rounded border border-pos-border bg-pos-surface p-2"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="text-pos-text">
                    {cuando(op.creadaEn)} · {formatCents(toCents(op.payload.tenders[0]?.amount_ars ?? "0") ?? 0)}
                  </span>
                  <span className="text-xs text-pos-muted">{ROTULO[op.estado]}</span>
                </div>
                <p className="mt-1 text-xs text-pos-text-2">
                  {op.ultimoError ?? AYUDA[op.estado]}
                </p>
                {descartando === op.clientOperationId ? (
                  <div className="mt-2 space-y-1">
                    <p className="text-xs text-pos-danger">
                      Esta venta puede haber entrado igual. Descartala sólo si confirmaste
                      en Ventas que no está.
                    </p>
                    <div className="flex gap-2">
                      <Button size="sm" variant="danger" onClick={() => void descartar(op)}>
                        Descartar igual
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => setDescartando(null)}>
                        No
                      </Button>
                    </div>
                  </div>
                ) : (
                  <div className="mt-2 flex flex-wrap gap-2">
                    <Button size="sm" variant="secondary" onClick={() => void reintentar(op)}>
                      Reintentar
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => onImprimir(op)}>
                      Imprimir comprobante
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => setDescartando(op.clientOperationId)}
                    >
                      Descartar
                    </Button>
                  </div>
                )}
              </li>
            ))}
          </ul>
          <Button size="sm" variant="ghost" onClick={exportar}>
            Exportar a un archivo
          </Button>
        </div>
      )}
    </div>
  );
}
