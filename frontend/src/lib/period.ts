// Cálculo de rangos de período para los filtros temporales (ventas/gastos).
// Única fuente de verdad — reemplaza los `getPeriodDates` duplicados por página.
// Convención: semana = lunes a domingo. Formateo LOCAL (sin conversión UTC),
// consistente con America/Argentina/Buenos_Aires asumido en todo el sistema.

export type PeriodPreset =
  | "today"
  | "yesterday"
  | "this_week"
  | "last_week"
  | "this_month"
  | "last_month";

export type PeriodValue =
  | { kind: "preset"; preset: PeriodPreset }
  | { kind: "year"; year: number }
  | { kind: "month"; year: number; month: number } // month: 1-12
  | { kind: "week"; start: string } // start: lunes "YYYY-MM-DD"
  | { kind: "day"; date: string }; // "YYYY-MM-DD"

export interface DateRangeStrings {
  from: string;
  to: string;
}

const pad = (n: number): string => String(n).padStart(2, "0");

export function fmt(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function parseLocal(iso: string): Date {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y ?? 1970, (m ?? 1) - 1, d ?? 1);
}

/** Lunes de la semana que contiene `d` (semana lunes-domingo). */
function mondayOf(d: Date): Date {
  const day = d.getDay(); // 0=domingo … 6=sábado
  const diff = day === 0 ? -6 : 1 - day;
  const monday = new Date(d.getFullYear(), d.getMonth(), d.getDate() + diff);
  return monday;
}

function addDays(d: Date, n: number): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
}

/** Último día del mes (month: 1-12). */
function endOfMonth(year: number, month: number): Date {
  return new Date(year, month, 0); // día 0 del mes siguiente = último del actual
}

const MONTH_NAMES = [
  "Enero",
  "Febrero",
  "Marzo",
  "Abril",
  "Mayo",
  "Junio",
  "Julio",
  "Agosto",
  "Septiembre",
  "Octubre",
  "Noviembre",
  "Diciembre",
];

export function resolvePeriod(value: PeriodValue): DateRangeStrings {
  const now = new Date();

  switch (value.kind) {
    case "preset": {
      switch (value.preset) {
        case "today":
          return { from: fmt(now), to: fmt(now) };
        case "yesterday": {
          const y = addDays(now, -1);
          return { from: fmt(y), to: fmt(y) };
        }
        case "this_week":
          return { from: fmt(mondayOf(now)), to: fmt(now) };
        case "last_week": {
          const lastMonday = addDays(mondayOf(now), -7);
          return { from: fmt(lastMonday), to: fmt(addDays(lastMonday, 6)) };
        }
        case "this_month":
          return {
            from: fmt(new Date(now.getFullYear(), now.getMonth(), 1)),
            to: fmt(now),
          };
        case "last_month":
          return {
            from: fmt(new Date(now.getFullYear(), now.getMonth() - 1, 1)),
            to: fmt(new Date(now.getFullYear(), now.getMonth(), 0)),
          };
      }
      break;
    }
    case "year":
      return {
        from: fmt(new Date(value.year, 0, 1)),
        to: fmt(new Date(value.year, 11, 31)),
      };
    case "month":
      return {
        from: fmt(new Date(value.year, value.month - 1, 1)),
        to: fmt(endOfMonth(value.year, value.month)),
      };
    case "week": {
      const start = parseLocal(value.start);
      return { from: value.start, to: fmt(addDays(start, 6)) };
    }
    case "day":
      return { from: value.date, to: value.date };
  }
  // Inalcanzable, pero TS exige retorno total.
  return { from: fmt(now), to: fmt(now) };
}

/**
 * Rango inmediatamente anterior comparable (para el StatCard "vs período anterior").
 * Para presets "en curso" (today/this_week/this_month) compara contra el período
 * completo anterior de la misma granularidad.
 */
export function resolvePreviousPeriod(value: PeriodValue): DateRangeStrings {
  const now = new Date();
  switch (value.kind) {
    case "preset": {
      switch (value.preset) {
        case "today":
        case "yesterday": {
          const ref =
            value.preset === "today" ? addDays(now, -1) : addDays(now, -2);
          return { from: fmt(ref), to: fmt(ref) };
        }
        case "this_week":
        case "last_week": {
          const baseMonday =
            value.preset === "this_week"
              ? addDays(mondayOf(now), -7)
              : addDays(mondayOf(now), -14);
          return { from: fmt(baseMonday), to: fmt(addDays(baseMonday, 6)) };
        }
        case "this_month":
        case "last_month": {
          const offset = value.preset === "this_month" ? 1 : 2;
          return {
            from: fmt(new Date(now.getFullYear(), now.getMonth() - offset, 1)),
            to: fmt(new Date(now.getFullYear(), now.getMonth() - offset + 1, 0)),
          };
        }
      }
      break;
    }
    case "year":
      return resolvePeriod({ kind: "year", year: value.year - 1 });
    case "month": {
      const prevMonth = value.month === 1 ? 12 : value.month - 1;
      const prevYear = value.month === 1 ? value.year - 1 : value.year;
      return resolvePeriod({ kind: "month", year: prevYear, month: prevMonth });
    }
    case "week": {
      const start = parseLocal(value.start);
      return resolvePeriod({ kind: "week", start: fmt(addDays(start, -7)) });
    }
    case "day": {
      const d = parseLocal(value.date);
      return resolvePeriod({ kind: "day", date: fmt(addDays(d, -1)) });
    }
  }
  return resolvePeriod(value);
}

const PRESET_LABELS: Record<PeriodPreset, string> = {
  today: "Hoy",
  yesterday: "Ayer",
  this_week: "Esta semana",
  last_week: "Semana pasada",
  this_month: "Este mes",
  last_month: "Mes pasado",
};

export function formatPeriodLabel(value: PeriodValue): string {
  switch (value.kind) {
    case "preset":
      return PRESET_LABELS[value.preset];
    case "year":
      return String(value.year);
    case "month":
      return `${MONTH_NAMES[value.month - 1]} ${value.year}`;
    case "week": {
      const start = parseLocal(value.start);
      const end = addDays(start, 6);
      return `Semana del ${start.getDate()}/${pad(start.getMonth() + 1)} al ${end.getDate()}/${pad(end.getMonth() + 1)}`;
    }
    case "day": {
      const d = parseLocal(value.date);
      return `${d.getDate()}/${pad(d.getMonth() + 1)}/${d.getFullYear()}`;
    }
  }
}

/** Etiqueta corta del período anterior, para el StatCard de comparación. */
export function previousPeriodShortLabel(value: PeriodValue): string {
  switch (value.kind) {
    case "preset":
      switch (value.preset) {
        case "today":
          return "vs ayer";
        case "yesterday":
          return "vs anteayer";
        case "this_week":
        case "last_week":
          return "vs semana anterior";
        default:
          return "vs mes anterior";
      }
    case "year":
      return `vs ${value.year - 1}`;
    case "month":
      return "vs mes anterior";
    case "week":
      return "vs semana anterior";
    case "day":
      return "vs día anterior";
  }
}

export const MONTH_LABELS = MONTH_NAMES;

/** Lista de meses (1-12) con label, para el navegador jerárquico. */
export function monthsOfYear(): { month: number; label: string }[] {
  return MONTH_NAMES.map((label, i) => ({ month: i + 1, label }));
}

/** Lunes de cada semana que cae dentro de un mes (year, month: 1-12). */
export function weeksOfMonth(year: number, month: number): string[] {
  const first = new Date(year, month - 1, 1);
  const last = endOfMonth(year, month);
  const starts: string[] = [];
  let cursor = mondayOf(first);
  // incluir semanas cuyo lunes cae antes/dentro del mes mientras la semana toque el mes
  while (cursor <= last) {
    starts.push(fmt(cursor));
    cursor = addDays(cursor, 7);
  }
  return starts;
}

/** True si el rango disponible empieza en un año anterior al actual. */
export function hasMultiYearData(minDateIso: string | null): boolean {
  if (!minDateIso) return false;
  return parseLocal(minDateIso).getFullYear() < new Date().getFullYear();
}

/** Años seleccionables entre min y hoy (descendente). */
export function selectableYears(minDateIso: string | null): number[] {
  const currentYear = new Date().getFullYear();
  if (!minDateIso) return [currentYear];
  const minYear = parseLocal(minDateIso).getFullYear();
  const years: number[] = [];
  for (let y = currentYear; y >= minYear; y--) years.push(y);
  return years;
}
