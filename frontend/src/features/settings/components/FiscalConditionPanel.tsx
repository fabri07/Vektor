"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { settingsService } from "@/services/settings.service";
import {
  FISCAL_CONDITION_OPTIONS,
  FISCAL_PRIVACY_NOTE,
  type FiscalCondition,
} from "@/lib/fiscalCondition";
import { useToastStore } from "@/stores/toastStore";

const OPTIONS: { value: FiscalCondition | ""; label: string; detail: string }[] = [
  ...FISCAL_CONDITION_OPTIONS,
  { value: "", label: "Prefiero no configurarlo", detail: "" },
];

export function FiscalConditionPanel() {
  const qc = useQueryClient();
  const toast = useToastStore();

  const { data: condition, isLoading } = useQuery({
    queryKey: ["fiscal-condition"],
    queryFn: () => settingsService.getFiscalCondition(),
  });

  const [value, setValue] = useState<FiscalCondition | "">("");
  const [synced, setSynced] = useState(false);

  useEffect(() => {
    if (!isLoading && !synced) {
      setValue(condition ?? "");
      setSynced(true);
    }
  }, [condition, isLoading, synced]);

  const saveMutation = useMutation({
    mutationFn: () => settingsService.updateFiscalCondition(value === "" ? null : value),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["fiscal-condition"] });
      qc.invalidateQueries({ queryKey: ["cash-preview"] });
      toast.add("Condición fiscal guardada.", "success");
    },
    onError: () => toast.add("No se pudo guardar la condición fiscal.", "error"),
  });

  if (isLoading) return null;

  return (
    <div className="space-y-6 pt-8">
      <div>
        <h3 className="text-base font-semibold text-vk-text-primary">Condición fiscal</h3>
        <p className="mt-1 text-sm text-vk-text-muted">
          Adapta la guía de comprobantes del cierre de caja. No cambia ningún cálculo
          y Véktor no valida comprobantes ni liquida impuestos.
        </p>
      </div>

      <div className="space-y-4 rounded-xl border border-vk-border-w bg-vk-surface-w p-5 shadow-vk-sm">
        <div className="space-y-2">
          {OPTIONS.map((opt) => (
            <label
              key={opt.value || "none"}
              className="flex cursor-pointer items-start gap-2 text-sm text-vk-text-secondary"
            >
              <input
                type="radio"
                name="fiscal-condition"
                checked={value === opt.value}
                onChange={() => setValue(opt.value)}
                className="mt-0.5"
              />
              <span>
                <span className="font-medium text-vk-text-primary">{opt.label}</span>
                {opt.detail ? (
                  <span className="block text-xs text-vk-text-muted">{opt.detail}</span>
                ) : null}
              </span>
            </label>
          ))}
        </div>

        <p className="rounded-lg border border-vk-border-w bg-vk-bg-light px-3 py-2 text-xs text-vk-text-muted">
          {FISCAL_PRIVACY_NOTE}
        </p>

        <button
          type="button"
          disabled={saveMutation.isPending}
          onClick={() => saveMutation.mutate()}
          className="rounded-full bg-vk-blue px-5 py-2 text-sm font-medium text-white hover:bg-vk-blue-hover transition-colors disabled:opacity-50"
        >
          {saveMutation.isPending ? "Guardando…" : "Guardar"}
        </button>
      </div>
    </div>
  );
}
