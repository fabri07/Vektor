"use client";

import { useEffect, useState } from "react";

import { VoidLastTicket } from "@/features/pos/VoidLastTicket";
import { formatTicketDate } from "@/features/pos/ticketTemplate";
import { formatCents, toCents } from "@/lib/pos/cart";
import { type PosOperationSummary, posService } from "@/services/pos.service";

interface RecentTicketsProps {
  /** Cambia con cada cobro o anulación: la lista se vuelve a pedir. */
  refreshKey: string;
  canVoid: boolean;
  onReprint: (operationId: string) => void;
  onVoided: () => void;
}

/**
 * "Últimos tickets" (B12): reimprimir sin depender de que la página no se haya
 * recargado. Un cajero ve sólo los suyos (lo filtra el servidor).
 */
export function RecentTickets({ refreshKey, canVoid, onReprint, onVoided }: RecentTicketsProps) {
  const [abierto, setAbierto] = useState(false);
  const [items, setItems] = useState<PosOperationSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!abierto) return;
    let vigente = true;
    posService
      .listOperations()
      .then((r) => {
        if (vigente) {
          setItems(r);
          setError(null);
        }
      })
      .catch(() => vigente && setError("No se pudieron traer los tickets."));
    return () => {
      vigente = false;
    };
  }, [abierto, refreshKey]);

  return (
    <div className="border-t border-vk-border-w pt-3 text-sm">
      <button
        type="button"
        className="text-vk-text-secondary hover:text-vk-text-primary"
        onClick={() => setAbierto((v) => !v)}
      >
        {abierto ? "Ocultar últimos tickets" : "Últimos tickets"}
      </button>
      {abierto ? (
        <div className="mt-2 max-h-72 space-y-2 overflow-auto">
          {error ? <p className="text-vk-danger">{error}</p> : null}
          {items?.length === 0 ? <p className="text-vk-text-muted">Todavía no hay tickets.</p> : null}
          {items?.map((t) => (
            <div key={t.id} className="space-y-1 rounded-lg border border-vk-border-w p-2">
              <div className="flex items-center justify-between">
                <span className="font-mono text-xs text-vk-text-muted">
                  {t.number} · {formatTicketDate(t.operation_date).slice(-5)}
                </span>
                <span className={t.status === "VOIDED" ? "text-vk-text-muted line-through" : "font-semibold"}>
                  {formatCents(toCents(t.total_ars) ?? 0)}
                </span>
              </div>
              <div className="flex items-center gap-3">
                <button
                  type="button"
                  className="text-vk-blue underline"
                  onClick={() => onReprint(t.id)}
                >
                  Reimprimir
                </button>
                {t.status === "VOIDED" ? <span className="text-xs text-vk-text-muted">Anulado</span> : null}
              </div>
              {canVoid && t.status === "COMPLETED" ? (
                <VoidLastTicket
                  operation={t}
                  label="Anular"
                  onVoided={() => {
                    setItems((prev) =>
                      prev?.map((x) => (x.id === t.id ? { ...x, status: "VOIDED" } : x)) ?? null,
                    );
                    onVoided();
                  }}
                />
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
