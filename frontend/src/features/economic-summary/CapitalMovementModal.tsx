"use client";

import { useEffect, useState } from "react";
import { useMutation, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { useToastStore } from "@/stores/toastStore";
import { ownerCapitalService, type CapitalKind } from "@/services/owner-capital.service";
import { PAYMENT_LABELS } from "@/lib/payment";

export const CAPITAL_KIND_LABELS: Record<CapitalKind, string> = {
  aporte: "Aporte",
  retiro: "Retiro",
  prestamo_socio: "Préstamo de socio",
  devolucion_prestamo: "Devolución de préstamo",
};

const KINDS: CapitalKind[] = ["aporte", "retiro", "prestamo_socio", "devolucion_prestamo"];

// Medios que acepta el backend, etiquetados desde la fuente única PAYMENT_LABELS.
const PAYMENT_METHODS = ["cash", "transfer", "debit_card", "credit_card", "qr", "account"].map(
  (value) => ({
    value,
    label: value === "cash" ? "Efectivo (caja)" : PAYMENT_LABELS[value] ?? value,
  }),
);

// Fecha LOCAL, no `toISOString()`: esa es la fecha UTC, que entre las 21:00 y
// la medianoche de Argentina ya es mañana — y el backend la rechaza por futura.
function todayISO(): string {
  const d = new Date();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${mm}-${dd}`;
}

/** Lo que cambia cuando entra o se anula un movimiento de capital. */
export async function invalidateCapitalQueries(queryClient: QueryClient): Promise<void> {
  await Promise.all(
    [
      ["capital-summary"],
      ["capital-movements"],
      ["economic-summary"],
      ["expenses-entries"],
      ["expenses-date-range"],
      ["breakdown"],
      ["forecast"],
      ["momentum"],
    ].map((queryKey) => queryClient.invalidateQueries({ queryKey })),
  );
}

/**
 * Alta de un movimiento de capital de un socio (aporte, retiro, préstamo o su
 * devolución). Pide PIN al confirmar (vía interceptor 428).
 */
export function CapitalMovementModal({
  isOpen,
  onClose,
}: {
  isOpen: boolean;
  onClose: () => void;
}) {
  const [kind, setKind] = useState<CapitalKind>("aporte");
  const [amount, setAmount] = useState("");
  const [date, setDate] = useState(todayISO());
  const [method, setMethod] = useState("cash");
  const [partner, setPartner] = useState("");
  const [notes, setNotes] = useState("");
  const [error, setError] = useState<string | null>(null);
  // Una clave por apertura: un reintento del mismo formulario no duplica.
  const [idempotencyKey, setIdempotencyKey] = useState(() => crypto.randomUUID());
  const queryClient = useQueryClient();
  const toast = useToastStore((s) => s.add);

  useEffect(() => {
    if (isOpen) {
      setKind("aporte");
      setAmount("");
      setDate(todayISO());
      setMethod("cash");
      setPartner("");
      setNotes("");
      setError(null);
      setIdempotencyKey(crypto.randomUUID());
    }
  }, [isOpen]);

  const mutation = useMutation({
    mutationFn: () =>
      ownerCapitalService.create(
        {
          kind,
          amount: Number(amount),
          occurred_on: date,
          payment_method: method,
          partner_name: partner.trim() || null,
          notes: notes.trim() || null,
        },
        idempotencyKey,
      ),
    onSuccess: async () => {
      toast("Movimiento registrado.", "success");
      await invalidateCapitalQueries(queryClient);
      onClose();
    },
    onError: () => toast("No se pudo registrar el movimiento.", "error"),
  });

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const value = Number(amount);
    if (!Number.isFinite(value) || value <= 0) {
      setError("Ingresá un monto mayor a 0.");
      return;
    }
    setError(null);
    mutation.mutate();
  }

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="Registrar movimiento de socio" size="md">
      <form className="space-y-4" onSubmit={handleSubmit}>
        <p className="text-sm text-vk-text-secondary">
          Plata que un socio pone en el negocio o saca de él. No es venta ni gasto: no
          cambia el resultado ni el margen. Si fue en efectivo, sí cuenta en la caja
          esperada del arqueo.
        </p>

        <fieldset>
          <legend className="mb-1.5 text-sm font-medium text-vk-text-primary">Tipo</legend>
          <div className="grid grid-cols-2 gap-2">
            {KINDS.map((k) => (
              <button
                key={k}
                type="button"
                aria-pressed={kind === k}
                onClick={() => setKind(k)}
                className={
                  kind === k
                    ? "rounded-lg border border-vk-blue bg-vk-blue/10 px-3 py-2 text-sm font-medium text-vk-text-primary"
                    : "rounded-lg border border-vk-border-w bg-vk-surface-w px-3 py-2 text-sm text-vk-text-secondary hover:bg-vk-bg-light"
                }
              >
                {CAPITAL_KIND_LABELS[k]}
              </button>
            ))}
          </div>
        </fieldset>

        <Input
          label="Monto"
          type="number"
          inputMode="decimal"
          min="0"
          step="0.01"
          placeholder="50000"
          value={amount}
          onChange={(e) => setAmount(e.target.value)}
        />

        <div className="grid grid-cols-2 gap-4">
          <Input
            label="Fecha"
            type="date"
            max={todayISO()}
            value={date}
            onChange={(e) => setDate(e.target.value)}
          />
          <Select
            label="Medio de pago"
            options={PAYMENT_METHODS}
            value={method}
            onChange={(v) => setMethod(v)}
          />
        </div>

        <Input
          label="Socio (opcional)"
          type="text"
          placeholder="Ej: Juan"
          maxLength={120}
          value={partner}
          onChange={(e) => setPartner(e.target.value)}
        />

        <Input
          label="Nota (opcional)"
          type="text"
          placeholder="Ej: retiro de octubre"
          maxLength={1000}
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
        />

        {error && (
          <p className="rounded-lg border border-vk-danger/20 bg-vk-danger-bg px-3 py-2 text-sm text-vk-danger">
            {error}
          </p>
        )}

        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" variant="secondary" size="sm" onClick={onClose}>
            Cancelar
          </Button>
          <Button type="submit" size="sm" loading={mutation.isPending}>
            Registrar
          </Button>
        </div>
      </form>
    </Modal>
  );
}
