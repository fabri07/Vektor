"use client";

import { useState } from "react";
import axios from "axios";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Modal } from "@/components/ui/Modal";
import { cashCloseService } from "@/services/cashClose.service";
import { paymentLabel } from "@/lib/payment";
import { useToastStore } from "@/stores/toastStore";

function formatARS(value: number): string {
  return new Intl.NumberFormat("es-AR", {
    style: "currency",
    currency: "ARS",
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  }).format(value);
}

function todayISO(): string {
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

interface CashCloseModalProps {
  isOpen: boolean;
  onClose: () => void;
}

export function CashCloseModal({ isOpen, onClose }: CashCloseModalProps) {
  const qc = useQueryClient();
  const toast = useToastStore();
  const closeDate = todayISO();

  const [counted, setCounted] = useState<string>("");
  const [notes, setNotes] = useState<string>("");
  const [byMethodOpen, setByMethodOpen] = useState(false);
  // Contado por método (opcional). Key = payment_method, value = string del input.
  const [countedByMethod, setCountedByMethod] = useState<Record<string, string>>({});

  const { data: preview, isLoading } = useQuery({
    queryKey: ["cash-preview", closeDate],
    queryFn: () => cashCloseService.getPreview(closeDate),
    enabled: isOpen,
    staleTime: 30 * 1000,
  });

  const createMutation = useMutation({
    mutationFn: () => {
      // Si el usuario desglosó por método, lo mandamos; el total contado es la suma.
      const byMethod: Record<string, number> = {};
      let hasByMethod = false;
      for (const [method, raw] of Object.entries(countedByMethod)) {
        const num = parseFloat(raw);
        if (!isNaN(num)) {
          byMethod[method] = num;
          hasByMethod = true;
        }
      }
      const totalCounted = hasByMethod
        ? Object.values(byMethod).reduce((s, v) => s + v, 0)
        : parseFloat(counted) || 0;
      return cashCloseService.createClose({
        close_date: closeDate,
        counted_total_ars: totalCounted,
        counted_by_method: hasByMethod ? byMethod : undefined,
        notes: notes.trim() || null,
      });
    },
    onSuccess: async () => {
      toast.add("Cierre de caja registrado.", "success");
      await qc.invalidateQueries({ queryKey: ["cash-preview"] });
      await qc.invalidateQueries({ queryKey: ["cash-closes-today"] });
      setCounted("");
      setNotes("");
      setCountedByMethod({});
      setByMethodOpen(false);
      onClose();
    },
    onError: (err: unknown) => {
      if (axios.isAxiosError(err) && err.response?.status === 409) {
        toast.add("Ya registraste el cierre de caja de hoy.", "error");
      } else {
        toast.add("No se pudo registrar el cierre.", "error");
      }
    },
  });

  const expected = preview?.expected_total_ars ?? 0;
  // Total contado: suma del desglose por método si se está usando, si no el total simple.
  const byMethodValues = Object.values(countedByMethod)
    .map((v) => parseFloat(v))
    .filter((n) => !isNaN(n));
  const usingByMethod = byMethodOpen && byMethodValues.length > 0;
  const countedNum = usingByMethod
    ? byMethodValues.reduce((s, v) => s + v, 0)
    : parseFloat(counted);
  const hasCounted = usingByMethod || !isNaN(countedNum);
  const difference = hasCounted ? countedNum - expected : 0;
  const diffColor =
    difference > 0
      ? "text-vk-success"
      : difference < 0
        ? "text-vk-danger"
        : "text-vk-text-secondary";

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="Cierre de caja" size="md">
      {isLoading ? (
        <p className="py-8 text-center text-sm text-vk-text-muted">Calculando…</p>
      ) : preview?.already_closed ? (
        <p className="py-8 text-center text-sm text-vk-text-secondary">
          Ya registraste el cierre de caja de hoy.
        </p>
      ) : (
        <div className="space-y-5">
          {/* Esperado por método */}
          <div>
            <p className="mb-2 text-xs uppercase tracking-wide text-vk-text-muted">
              Esperado según el sistema
            </p>
            {preview && preview.breakdown.length > 0 ? (
              <div className="space-y-1.5">
                {preview.breakdown.map((b) => (
                  <div
                    key={b.payment_method}
                    className="flex justify-between text-sm"
                  >
                    <span className="text-vk-text-secondary">
                      {paymentLabel(b.payment_method)}
                    </span>
                    <span className="tabular-nums text-vk-text-primary">
                      {formatARS(b.expected_ars)}
                    </span>
                  </div>
                ))}
                <div className="mt-2 flex justify-between border-t border-vk-border-w pt-2 text-sm font-semibold">
                  <span className="text-vk-text-primary">Total esperado</span>
                  <span className="tabular-nums text-vk-text-primary">
                    {formatARS(expected)}
                  </span>
                </div>
              </div>
            ) : (
              <p className="text-sm text-vk-text-muted">
                No hay ventas registradas hoy. El esperado es {formatARS(0)}.
              </p>
            )}
          </div>

          {/* Contado */}
          <div>
            <div className="mb-1.5 flex items-center justify-between">
              <label
                htmlFor="counted"
                className="text-sm font-medium text-vk-text-primary"
              >
                ¿Cuánto contaste en caja?
              </label>
              {preview && preview.breakdown.length > 0 && (
                <button
                  type="button"
                  onClick={() => setByMethodOpen((o) => !o)}
                  className="text-xs text-vk-blue hover:underline"
                >
                  {byMethodOpen ? "Ingresar total" : "Desglosar por método"}
                </button>
              )}
            </div>

            {byMethodOpen && preview ? (
              <div className="space-y-2">
                {preview.breakdown.map((b) => (
                  <div key={b.payment_method} className="flex items-center gap-2">
                    <span className="w-32 text-sm text-vk-text-secondary">
                      {paymentLabel(b.payment_method)}
                    </span>
                    <div className="relative flex-1">
                      <span className="absolute left-3 top-1/2 -translate-y-1/2 text-sm text-vk-text-muted">
                        $
                      </span>
                      <input
                        type="number"
                        inputMode="decimal"
                        value={countedByMethod[b.payment_method] ?? ""}
                        onChange={(e) =>
                          setCountedByMethod((prev) => ({
                            ...prev,
                            [b.payment_method]: e.target.value,
                          }))
                        }
                        placeholder={String(b.expected_ars)}
                        className="w-full rounded-lg border border-vk-border-w bg-vk-night py-2 pl-7 pr-3 text-sm text-vk-text-primary focus:outline-none focus:ring-2 focus:ring-vk-blue/20"
                      />
                    </div>
                  </div>
                ))}
                {usingByMethod && (
                  <p className="text-right text-xs text-vk-text-muted">
                    Total contado: {formatARS(countedNum)}
                  </p>
                )}
              </div>
            ) : (
              <div className="relative">
                <span className="absolute left-3 top-1/2 -translate-y-1/2 text-sm text-vk-text-muted">
                  $
                </span>
                <input
                  id="counted"
                  type="number"
                  inputMode="decimal"
                  value={counted}
                  onChange={(e) => setCounted(e.target.value)}
                  placeholder="0"
                  className="w-full rounded-lg border border-vk-border-w bg-vk-night py-2 pl-7 pr-3 text-sm text-vk-text-primary focus:outline-none focus:ring-2 focus:ring-vk-blue/20"
                />
              </div>
            )}
          </div>

          {/* Diferencia en vivo */}
          {hasCounted && (
            <div className="flex items-center justify-between rounded-lg border border-vk-border-w bg-vk-surface-w px-4 py-3">
              <span className="text-sm text-vk-text-secondary">Diferencia</span>
              <span className={`text-lg font-semibold tabular-nums ${diffColor}`}>
                {difference > 0 ? "+" : ""}
                {formatARS(difference)}
              </span>
            </div>
          )}

          {/* Notas */}
          <div>
            <label
              htmlFor="cash-notes"
              className="mb-1.5 block text-sm font-medium text-vk-text-primary"
            >
              Notas (opcional)
            </label>
            <textarea
              id="cash-notes"
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              rows={2}
              maxLength={500}
              placeholder="Ej: faltó vuelto, propina incluida…"
              className="w-full rounded-lg border border-vk-border-w bg-vk-night px-3 py-2 text-sm text-vk-text-primary focus:outline-none focus:ring-2 focus:ring-vk-blue/20"
            />
          </div>

          <div className="flex justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded-full border border-vk-border-w px-5 py-2 text-sm font-medium text-vk-text-secondary hover:text-vk-text-primary"
            >
              Cancelar
            </button>
            <button
              type="button"
              disabled={!hasCounted || createMutation.isPending}
              onClick={() => createMutation.mutate()}
              className="rounded-full bg-vk-blue px-5 py-2 text-sm font-medium text-white disabled:opacity-50"
            >
              {createMutation.isPending ? "Guardando…" : "Cerrar caja"}
            </button>
          </div>
        </div>
      )}
    </Modal>
  );
}
