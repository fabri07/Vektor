"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { settingsService } from "@/services/settings.service";
import { useToastStore } from "@/stores/toastStore";

// 0=lunes … 6=domingo (consistente con el backend)
const DAY_LABELS: { value: number; label: string }[] = [
  { value: 0, label: "Lun" },
  { value: 1, label: "Mar" },
  { value: 2, label: "Mié" },
  { value: 3, label: "Jue" },
  { value: 4, label: "Vie" },
  { value: 5, label: "Sáb" },
  { value: 6, label: "Dom" },
];
const HOUR_OPTIONS = Array.from({ length: 24 }, (_, h) => h);
const DEFAULTS = { days: [0, 1, 2, 3, 4, 5], open: 9, close: 18 };

export function WorkSchedulePanel() {
  const qc = useQueryClient();
  const toast = useToastStore();

  const { data: schedule, isLoading } = useQuery({
    queryKey: ["work-schedule"],
    queryFn: () => settingsService.getWorkSchedule(),
  });

  const [days, setDays] = useState<number[]>(DEFAULTS.days);
  const [openHour, setOpenHour] = useState<number>(DEFAULTS.open);
  const [closeHour, setCloseHour] = useState<number>(DEFAULTS.close);
  const [synced, setSynced] = useState(false);

  useEffect(() => {
    if (schedule && !synced) {
      setDays(schedule.work_days);
      setOpenHour(schedule.work_open_hour);
      setCloseHour(schedule.work_close_hour);
      setSynced(true);
    }
  }, [schedule, synced]);

  const saveMutation = useMutation({
    mutationFn: () =>
      settingsService.updateWorkSchedule({
        work_days: days,
        work_open_hour: openHour,
        work_close_hour: closeHour,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["work-schedule"] });
      toast.add("Horario laboral guardado.", "success");
    },
    onError: () => toast.add("No se pudo guardar el horario.", "error"),
  });

  function toggleDay(day: number) {
    setDays((prev) =>
      prev.includes(day)
        ? prev.filter((d) => d !== day)
        : [...prev, day].sort((a, b) => a - b),
    );
  }

  const hasError = days.length === 0 || closeHour <= openHour;

  if (isLoading) {
    return (
      <div className="py-12 text-center text-sm text-vk-text-muted">
        Cargando horario…
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-base font-semibold text-vk-text-primary">
          Días y horario laboral
        </h3>
        <p className="mt-1 text-sm text-vk-text-muted">
          Definí cuándo abrís tu negocio. Véktor lo usa para recordarte el cierre
          de caja al final del día laboral.{" "}
          {schedule?.is_default && (
            <span className="text-vk-text-secondary">
              Usando valores por defecto:{" "}
              <strong className="text-vk-text-primary">Lun-Sáb, 09:00-18:00</strong>.
            </span>
          )}
        </p>
      </div>

      <div className="space-y-6 rounded-xl border border-vk-border-w bg-vk-surface-w p-5 shadow-vk-sm">
        <div>
          <label className="mb-3 block text-sm font-medium text-vk-text-primary">
            Días que abrís
          </label>
          <div className="flex flex-wrap gap-2">
            {DAY_LABELS.map((d) => {
              const isSelected = days.includes(d.value);
              return (
                <button
                  key={d.value}
                  type="button"
                  onClick={() => toggleDay(d.value)}
                  className={[
                    "rounded-lg border px-3 py-2 text-sm font-medium transition-colors",
                    isSelected
                      ? "border-vk-blue bg-vk-blue text-white"
                      : "border-vk-border-w bg-vk-surface-w text-vk-text-secondary hover:text-vk-text-primary",
                  ].join(" ")}
                >
                  {d.label}
                </button>
              );
            })}
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <label className="text-sm font-medium text-vk-text-primary">Abre</label>
          <select
            value={openHour}
            onChange={(e) => setOpenHour(Number(e.target.value))}
            className="rounded-lg border border-vk-border-w bg-vk-surface-w px-3 py-1.5 text-sm text-vk-text-primary focus:outline-none focus:ring-2 focus:ring-vk-blue/20"
          >
            {HOUR_OPTIONS.map((h) => (
              <option key={h} value={h}>
                {String(h).padStart(2, "0")}:00
              </option>
            ))}
          </select>
          <label className="text-sm font-medium text-vk-text-primary">Cierra</label>
          <select
            value={closeHour}
            onChange={(e) => setCloseHour(Number(e.target.value))}
            className="rounded-lg border border-vk-border-w bg-vk-surface-w px-3 py-1.5 text-sm text-vk-text-primary focus:outline-none focus:ring-2 focus:ring-vk-blue/20"
          >
            {HOUR_OPTIONS.map((h) => (
              <option key={h} value={h}>
                {String(h).padStart(2, "0")}:00
              </option>
            ))}
          </select>
        </div>

        {hasError && (
          <p className="rounded-lg border border-vk-danger/30 bg-vk-danger-bg px-4 py-2 text-xs text-vk-danger">
            {days.length === 0
              ? "Elegí al menos un día laboral."
              : "El horario de cierre debe ser posterior al de apertura."}
          </p>
        )}

        <div className="flex flex-wrap items-center gap-3 pt-2">
          <button
            type="button"
            disabled={hasError || saveMutation.isPending}
            onClick={() => saveMutation.mutate()}
            className="rounded-full bg-vk-blue px-5 py-2 text-sm font-medium text-white hover:bg-vk-blue-hover transition-colors disabled:opacity-50"
          >
            {saveMutation.isPending ? "Guardando…" : "Guardar"}
          </button>
        </div>
      </div>
    </div>
  );
}
