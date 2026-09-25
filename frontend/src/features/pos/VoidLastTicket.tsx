"use client";

import { useState } from "react";

import { Button } from "@/components/ui/Button";
import { type PosOperationResult, formatCents, toCents } from "@/lib/pos/cart";
import { cobroRejectionMessage, posErrorInfo } from "@/lib/pos/errors";
import { posService } from "@/services/pos.service";

interface VoidLastTicketProps {
  operation: PosOperationResult;
  onVoided: (op: PosOperationResult) => void;
}

/**
 * Anular el último ticket cobrado en esta caja (B10 desde la pantalla).
 *
 * Sin esto un cobro equivocado en la primera prueba sólo se corregía por API.
 * El servidor pide el PIN (428): lo resuelve el `PinGateModal` del layout y el
 * interceptor reintenta. El motivo se pide siempre; para un cajero es
 * obligatorio en el servidor.
 */
export function VoidLastTicket({ operation, onVoided }: VoidLastTicketProps) {
  const [abierto, setAbierto] = useState(false);
  const [motivo, setMotivo] = useState("");
  const [enviando, setEnviando] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (operation.status !== "COMPLETED") return null;

  if (!abierto) {
    return (
      <Button variant="ghost" size="sm" onClick={() => setAbierto(true)}>
        Anular último ticket ({formatCents(toCents(operation.total_ars) ?? 0)})
      </Button>
    );
  }

  async function anular(): Promise<void> {
    setEnviando(true);
    setError(null);
    try {
      const r = await posService.voidOperation(operation.id, motivo.trim());
      onVoided(r);
      setAbierto(false);
    } catch (e) {
      setError(cobroRejectionMessage(posErrorInfo(e)));
    } finally {
      setEnviando(false);
    }
  }

  return (
    <div className="space-y-2 rounded-lg border border-vk-border-w p-3">
      <p className="text-sm text-vk-text-primary">
        Anular el ticket de {formatCents(toCents(operation.total_ars) ?? 0)}. Vuelve el stock y la
        venta deja de contar.
      </p>
      <textarea
        aria-label="Motivo de la anulación"
        placeholder="Motivo (obligatorio)"
        className="w-full rounded-lg border border-vk-border-w bg-vk-surface-w p-2 text-sm text-vk-text-primary"
        value={motivo}
        onChange={(e) => setMotivo(e.target.value)}
      />
      {error ? <p className="text-sm text-vk-danger">{error}</p> : null}
      <div className="flex gap-2">
        <Button
          variant="danger"
          size="sm"
          loading={enviando}
          disabled={motivo.trim() === "" || enviando}
          onClick={() => void anular()}
        >
          Confirmar anulación
        </Button>
        <Button variant="ghost" size="sm" onClick={() => setAbierto(false)}>
          Cancelar
        </Button>
      </div>
    </div>
  );
}
