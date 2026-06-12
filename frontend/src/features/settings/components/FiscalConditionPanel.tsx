"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { settingsService, type FiscalCondition } from "@/services/settings.service";
import { useToastStore } from "@/stores/toastStore";

const OPTIONS: { value: FiscalCondition | ""; label: string; detail: string }[] = [
  {
    value: "registered",
    label: "Registrado",
    detail: "Monotributo o Responsable Inscripto ante ARCA (ex-AFIP).",
  },
  {
    value: "informal",
    label: "No registrado",
    detail: "El arqueo de caja es control interno, sin valor fiscal.",
  },
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
        <h3 className="text-base font-semibold text-vektor-white">Condición fiscal</h3>
        <p className="mt-1 text-sm text-vektor-muted">
          Adapta la guía de comprobantes del cierre de caja. No cambia ningún cálculo
          y Véktor no valida comprobantes ni liquida impuestos.
        </p>
      </div>

      <div className="vektor-card space-y-4 p-5">
        <div className="space-y-2">
          {OPTIONS.map((opt) => (
            <label
              key={opt.value || "none"}
              className="flex cursor-pointer items-start gap-2 text-sm text-vektor-body"
            >
              <input
                type="radio"
                name="fiscal-condition"
                checked={value === opt.value}
                onChange={() => setValue(opt.value)}
                className="mt-0.5"
              />
              <span>
                <span className="font-medium text-vektor-white">{opt.label}</span>
                {opt.detail ? (
                  <span className="block text-xs text-vektor-muted">{opt.detail}</span>
                ) : null}
              </span>
            </label>
          ))}
        </div>

        <button
          type="button"
          disabled={saveMutation.isPending}
          onClick={() => saveMutation.mutate()}
          className="rounded-full bg-[var(--vektor-blue)] px-5 py-2 text-sm font-medium text-white disabled:opacity-50"
        >
          {saveMutation.isPending ? "Guardando…" : "Guardar"}
        </button>
      </div>
    </div>
  );
}
