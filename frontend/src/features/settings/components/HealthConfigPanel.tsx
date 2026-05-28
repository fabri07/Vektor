"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { settingsService } from "@/services/settings.service";
import { useToastStore } from "@/stores/toastStore";

const DEFAULTS = { target: 35, warning: 20 };
const VERTICAL_DEFAULTS = "35% objetivo / 20% alerta (valores por vertical)";

function SliderField({
  label,
  hint,
  value,
  min,
  max,
  onChange,
}: {
  label: string;
  hint: string;
  value: number;
  min: number;
  max: number;
  onChange: (v: number) => void;
}) {
  return (
    <div>
      <div className="flex items-baseline justify-between">
        <label className="text-sm font-medium text-vektor-white">{label}</label>
        <span className="text-xl font-semibold text-vektor-white tabular-nums">
          {value}%
        </span>
      </div>
      <p className="mb-3 text-xs text-vektor-muted">{hint}</p>
      <input
        type="range"
        min={min}
        max={max}
        step={1}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="h-1.5 w-full cursor-pointer appearance-none rounded-full bg-vektor-surface accent-[var(--vektor-blue)]"
      />
      <div className="mt-1 flex justify-between text-[10px] text-vektor-muted">
        <span>{min}%</span>
        <span>{max}%</span>
      </div>
    </div>
  );
}

export function HealthConfigPanel() {
  const qc = useQueryClient();
  const toast = useToastStore();

  const { data: config, isLoading } = useQuery({
    queryKey: ["health-config"],
    queryFn: () => settingsService.getHealthConfig(),
  });

  const [target, setTarget] = useState<number>(DEFAULTS.target);
  const [warning, setWarning] = useState<number>(DEFAULTS.warning);
  const [synced, setSynced] = useState(false);

  // Sincronizar sliders con el server state cuando llega la respuesta.
  useEffect(() => {
    if (config && !synced) {
      setTarget(Math.round(config.target_margin_pct));
      setWarning(Math.round(config.warning_margin_pct));
      setSynced(true);
    }
  }, [config, synced]);

  const saveMutation = useMutation({
    mutationFn: () =>
      settingsService.updateHealthConfig(target, warning),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["health-config"] });
      toast.add("Configuración de margen guardada.", "success");
    },
    onError: () => toast.add("No se pudo guardar la configuración.", "error"),
  });

  const resetMutation = useMutation({
    mutationFn: () => settingsService.resetHealthConfig(),
    onSuccess: (data) => {
      if (data) {
        setTarget(Math.round(data.target_margin_pct));
        setWarning(Math.round(data.warning_margin_pct));
        setSynced(false);
      }
      qc.invalidateQueries({ queryKey: ["health-config"] });
      toast.add("Valores de margen restaurados a los predeterminados.", "success");
    },
    onError: () => toast.add("No se pudo restaurar la configuración.", "error"),
  });

  const hasError = warning > target;

  if (isLoading) {
    return (
      <div className="py-12 text-center text-sm text-vektor-muted">
        Cargando configuración…
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-base font-semibold text-vektor-white">
          Objetivo de margen
        </h3>
        <p className="mt-1 text-sm text-vektor-muted">
          Configurá los umbrales de margen bruto que Véktor usa para calcular la
          dimensión "Márgenes" en tu score de salud. Los demás factores (caja,
          inventario, proveedores, crecimiento) siguen los benchmarks del rubro.{" "}
          {!config?.is_custom && (
            <span className="text-vektor-body">
              Usando valores por defecto:{" "}
              <strong className="text-vektor-white">{VERTICAL_DEFAULTS}</strong>.
            </span>
          )}
        </p>
      </div>

      <div className="vektor-card space-y-6 p-5">
        <SliderField
          label="Margen objetivo"
          hint="Por encima de este valor el score de márgenes es ≥ 90. Es tu meta de rentabilidad bruta."
          value={target}
          min={5}
          max={80}
          onChange={(v) => setTarget(Math.max(v, warning))}
        />
        <SliderField
          label="Umbral de alerta"
          hint="Por debajo de este valor el score de márgenes entra en zona de advertencia."
          value={warning}
          min={1}
          max={79}
          onChange={(v) => setWarning(Math.min(v, target))}
        />

        {hasError && (
          <p className="rounded-lg border border-red-700 bg-red-950/40 px-4 py-2 text-xs text-red-400">
            El umbral de alerta no puede superar el objetivo.
          </p>
        )}

        <div className="flex flex-wrap items-center gap-3 pt-2">
          <button
            type="button"
            disabled={hasError || saveMutation.isPending}
            onClick={() => saveMutation.mutate()}
            className="rounded-full bg-[var(--vektor-blue)] px-5 py-2 text-sm font-medium text-white disabled:opacity-50"
          >
            {saveMutation.isPending ? "Guardando…" : "Guardar"}
          </button>
          {config?.is_custom && (
            <button
              type="button"
              disabled={resetMutation.isPending}
              onClick={() => resetMutation.mutate()}
              className="rounded-full border border-vektor-border px-5 py-2 text-sm font-medium text-vektor-body hover:text-vektor-white disabled:opacity-50"
            >
              {resetMutation.isPending ? "Restaurando…" : "Restablecer predeterminados"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
