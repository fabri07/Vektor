"use client";

import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { useToastStore } from "@/stores/toastStore";
import { expensesService } from "@/services/expenses.service";
import { PAYMENT_LABELS } from "@/lib/payment";

// Métodos que acepta el backend (ProfitWithdrawalRequest), etiquetados desde la
// fuente única PAYMENT_LABELS. "cash" sale de caja (default).
const PAYMENT_METHODS = ["cash", "transfer", "debit_card", "credit_card", "qr", "account"].map(
  (value) => ({
    value,
    label: value === "cash" ? "Efectivo (sale de caja)" : PAYMENT_LABELS[value] ?? value,
  }),
);

function todayISO(): string {
  return new Date().toISOString().slice(0, 10);
}

/**
 * Modal de "Retirar ganancias anticipadas": registra el sueldo/retiro del dueño
 * como gasto PAYROLL/OPEX. Pide PIN al confirmar (vía interceptor 428).
 */
export function ProfitWithdrawalModal({
  isOpen,
  onClose,
}: {
  isOpen: boolean;
  onClose: () => void;
}) {
  const [amount, setAmount] = useState("");
  const [date, setDate] = useState(todayISO());
  const [method, setMethod] = useState("cash");
  const [notes, setNotes] = useState("");
  const [error, setError] = useState<string | null>(null);
  const queryClient = useQueryClient();
  const toast = useToastStore((s) => s.add);

  useEffect(() => {
    if (isOpen) {
      setAmount("");
      setDate(todayISO());
      setMethod("cash");
      setNotes("");
      setError(null);
    }
  }, [isOpen]);

  const mutation = useMutation({
    mutationFn: () =>
      expensesService.profitWithdrawal({
        amount: Number(amount),
        withdrawal_date: date,
        payment_method: method,
        notes: notes.trim() || null,
      }),
    onSuccess: async () => {
      toast("Retiro registrado.", "success");
      // Claves reales usadas por las pantallas de gastos/balance/dashboard.
      await Promise.all(
        [
          ["economic-summary"],
          ["expenses-entries"],
          ["expenses-date-range"],
          ["breakdown"],
          ["forecast"],
          ["momentum"],
        ].map((queryKey) => queryClient.invalidateQueries({ queryKey })),
      );
      onClose();
    },
    onError: () => toast("No se pudo registrar el retiro.", "error"),
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
    <Modal isOpen={isOpen} onClose={onClose} title="Retirar ganancias anticipadas" size="md">
      <form className="space-y-4" onSubmit={handleSubmit}>
        <p className="text-sm text-vk-text-secondary">
          Registra el retiro/sueldo que te pagás como dueño. Se carga como gasto
          (categoría Sueldos) y descuenta del resultado.
        </p>

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
            label="Forma de pago"
            options={PAYMENT_METHODS}
            value={method}
            onChange={(v) => setMethod(v)}
          />
        </div>

        <Input
          label="Nota (opcional)"
          type="text"
          placeholder="Ej: retiro de octubre"
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
            Registrar retiro
          </Button>
        </div>
      </form>
    </Modal>
  );
}
