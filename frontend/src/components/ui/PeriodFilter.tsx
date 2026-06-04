"use client";

import { useState } from "react";
import {
  type PeriodValue,
  type PeriodPreset,
  formatPeriodLabel,
  hasMultiYearData,
  selectableYears,
  monthsOfYear,
  weeksOfMonth,
  fmt,
} from "@/lib/period";

interface DateRange {
  min_date: string | null;
  max_date: string | null;
}

interface PeriodFilterProps {
  value: PeriodValue;
  onChange: (value: PeriodValue) => void;
  availableRange: DateRange | null;
}

const PRESETS: { preset: PeriodPreset; label: string }[] = [
  { preset: "today", label: "Hoy" },
  { preset: "yesterday", label: "Ayer" },
  { preset: "this_week", label: "Esta semana" },
  { preset: "last_week", label: "Semana pasada" },
  { preset: "this_month", label: "Este mes" },
  { preset: "last_month", label: "Mes pasado" },
];

const chipBase =
  "rounded-full px-3 py-1 text-xs font-medium transition-colors focus:outline-none focus:ring-2 focus:ring-vk-blue/20";
const chipActive = "bg-vk-blue text-white";
const chipIdle =
  "border border-vk-border-w bg-vk-surface-w text-vk-text-muted hover:text-vk-text-primary";

export function PeriodFilter({
  value,
  onChange,
  availableRange,
}: PeriodFilterProps) {
  const minDate = availableRange?.min_date ?? null;
  const showYearNav = hasMultiYearData(minDate);
  const [showCustom, setShowCustom] = useState(false);

  const isActivePreset = (preset: PeriodPreset) =>
    value.kind === "preset" && value.preset === preset;

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm text-vk-text-muted">Período:</span>
        {PRESETS.map((p) => (
          <button
            key={p.preset}
            type="button"
            onClick={() => {
              setShowCustom(false);
              onChange({ kind: "preset", preset: p.preset });
            }}
            className={`${chipBase} ${isActivePreset(p.preset) ? chipActive : chipIdle}`}
          >
            {p.label}
          </button>
        ))}
        <button
          type="button"
          onClick={() => setShowCustom((s) => !s)}
          className={`${chipBase} ${value.kind !== "preset" ? chipActive : chipIdle}`}
        >
          {value.kind !== "preset" ? formatPeriodLabel(value) : "Elegir fecha…"}
        </button>
      </div>

      {showCustom && (
        <HierarchicalNav
          value={value}
          onChange={(v) => {
            onChange(v);
          }}
          minDate={minDate}
          showYearNav={showYearNav}
        />
      )}
    </div>
  );
}

interface NavProps {
  value: PeriodValue;
  onChange: (value: PeriodValue) => void;
  minDate: string | null;
  showYearNav: boolean;
}

/** Navegación año → mes → semana → día. Sin >1 año de datos: arranca en el año actual. */
function HierarchicalNav({ value, onChange, minDate, showYearNav }: NavProps) {
  const currentYear = new Date().getFullYear();
  const initialYear =
    value.kind === "year" || value.kind === "month"
      ? value.year
      : currentYear;
  const [year, setYear] = useState<number>(initialYear);
  const [month, setMonth] = useState<number | null>(
    value.kind === "month" ? value.month : null,
  );

  const years = selectableYears(minDate);

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-vk-border-w bg-vk-surface-w p-3">
      {/* Año */}
      {showYearNav && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="w-14 text-xs text-vk-text-muted">Año</span>
          {years.map((y) => (
            <button
              key={y}
              type="button"
              onClick={() => {
                setYear(y);
                setMonth(null);
                onChange({ kind: "year", year: y });
              }}
              className={`${chipBase} ${year === y && value.kind === "year" ? chipActive : chipIdle}`}
            >
              {y}
            </button>
          ))}
        </div>
      )}

      {/* Mes */}
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="w-14 text-xs text-vk-text-muted">Mes</span>
        {monthsOfYear().map((m) => (
          <button
            key={m.month}
            type="button"
            onClick={() => {
              setMonth(m.month);
              onChange({ kind: "month", year, month: m.month });
            }}
            className={`${chipBase} ${
              month === m.month &&
              (value.kind === "month" || value.kind === "week" || value.kind === "day")
                ? chipActive
                : chipIdle
            }`}
          >
            {m.label.slice(0, 3)}
          </button>
        ))}
      </div>

      {/* Semana / Día (solo cuando hay mes elegido) */}
      {month !== null && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="w-14 text-xs text-vk-text-muted">Semana</span>
          {weeksOfMonth(year, month).map((start, idx) => (
            <button
              key={start}
              type="button"
              onClick={() => onChange({ kind: "week", start })}
              className={`${chipBase} ${
                value.kind === "week" && value.start === start ? chipActive : chipIdle
              }`}
            >
              Sem {idx + 1}
            </button>
          ))}
        </div>
      )}

      {month !== null && (
        <div className="flex items-center gap-2">
          <span className="w-14 text-xs text-vk-text-muted">Día</span>
          <input
            type="date"
            value={value.kind === "day" ? value.date : ""}
            min={minDate ?? undefined}
            max={fmt(new Date())}
            onChange={(e) => {
              if (e.target.value) onChange({ kind: "day", date: e.target.value });
            }}
            className="rounded-lg border border-vk-border-w bg-vk-night px-3 py-1 text-sm text-vk-text-primary focus:outline-none focus:ring-2 focus:ring-vk-blue/20"
          />
        </div>
      )}
    </div>
  );
}
