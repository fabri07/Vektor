/**
 * Catálogos cerrados del screening de la solicitud de acceso.
 *
 * Un solo lugar con `value` + `label`: los valores alimentan los enums de Zod
 * (`validation/accessRequest.ts`) y las etiquetas alimentan la UI
 * (`features/access-request/`). Separarlos en dos archivos los desincroniza —
 * y un valor que la UI ofrece pero el schema no acepta es un 422 que el
 * usuario no puede explicarse.
 *
 * Espejo literal de los `StrEnum` de `backend/app/domain/access_request.py`
 * (más `main_concern`, que vive en `app/schemas/onboarding.py::MAIN_CONCERN_PATTERN`).
 */

export interface Choice<T extends string> {
  value: T;
  label: string;
  /** Aclaración corta, opcional: solo donde la etiqueta sola es ambigua. */
  detail?: string;
}

// ── Tu negocio ────────────────────────────────────────────────────────────────

export type YearsOperating = "lt_6m" | "6m_2y" | "2y_5y" | "gt_5y";

export const YEARS_OPERATING_OPTIONS: readonly Choice<YearsOperating>[] = [
  { value: "lt_6m", label: "Menos de 6 meses" },
  { value: "6m_2y", label: "Entre 6 meses y 2 años" },
  { value: "2y_5y", label: "Entre 2 y 5 años" },
  { value: "gt_5y", label: "Más de 5 años" },
] as const;

export type StaffSize = "solo" | "2_5" | "6_15" | "gt_15";

export const STAFF_SIZE_OPTIONS: readonly Choice<StaffSize>[] = [
  { value: "solo", label: "Trabajo por mi cuenta" },
  { value: "2_5", label: "2 a 5" },
  { value: "6_15", label: "6 a 15" },
  { value: "gt_15", label: "Más de 15" },
] as const;

export type MainConcern = "MARGIN" | "STOCK" | "CASH";

export const MAIN_CONCERN_OPTIONS: readonly Choice<MainConcern>[] = [
  {
    value: "MARGIN",
    label: "Margen",
    detail: "Cuánto deja realmente cada venta o producto.",
  },
  {
    value: "STOCK",
    label: "Stock",
    detail: "Qué reponer, qué sobra y cuánto capital está inmovilizado.",
  },
  {
    value: "CASH",
    label: "Caja",
    detail: "Con cuánto efectivo podés contar en los próximos días.",
  },
] as const;

export type RevenueBand = "lt_3m" | "3m_10m" | "10m_30m" | "gt_30m" | "no_contesta";

export const REVENUE_BAND_OPTIONS: readonly Choice<RevenueBand>[] = [
  { value: "lt_3m", label: "Menos de $3M" },
  { value: "3m_10m", label: "Entre $3M y $10M" },
  { value: "10m_30m", label: "Entre $10M y $30M" },
  { value: "gt_30m", label: "Más de $30M" },
  { value: "no_contesta", label: "Prefiero no decirlo" },
] as const;

// ── Tu info ───────────────────────────────────────────────────────────────────

export type RecordsFormat = "papel" | "planilla" | "sistema" | "mixto" | "ninguno";

export const RECORDS_FORMAT_OPTIONS: readonly Choice<RecordsFormat>[] = [
  { value: "papel", label: "Cuaderno o papel" },
  { value: "planilla", label: "Excel o Google Sheets" },
  { value: "sistema", label: "Un sistema de gestión o facturación" },
  { value: "mixto", label: "Una mezcla de varias cosas" },
  { value: "ninguno", label: "No guardo registros" },
] as const;

export type HistoryDepth = "lt_6m" | "6m_1y" | "1y_3y" | "gt_3y" | "ninguno";

export const HISTORY_DEPTH_OPTIONS: readonly Choice<HistoryDepth>[] = [
  { value: "lt_6m", label: "Menos de 6 meses" },
  { value: "6m_1y", label: "Entre 6 meses y 1 año" },
  { value: "1y_3y", label: "Entre 1 y 3 años" },
  { value: "gt_3y", label: "Más de 3 años" },
  { value: "ninguno", label: "No tengo historial" },
] as const;

export type CanShareFiles = "si_ordenados" | "si_desprolijos" | "no";

export const CAN_SHARE_FILES_OPTIONS: readonly Choice<CanShareFiles>[] = [
  { value: "si_ordenados", label: "Sí, están ordenados" },
  { value: "si_desprolijos", label: "Sí, aunque necesitan orden" },
  { value: "no", label: "No los tengo en formato digital" },
] as const;

// ── Cómo querés usar Véktor ───────────────────────────────────────────────────

export type RequestedPlan = "free" | "premium";

export const REQUESTED_PLAN_OPTIONS: readonly Choice<RequestedPlan>[] = [
  {
    value: "free",
    label: "Plan Gratuito",
    detail: "Quiero probar las funciones disponibles sin costo.",
  },
  {
    value: "premium",
    label: "Premium",
    detail:
      "Quiero recibir novedades y evaluar las funciones avanzadas cuando estén disponibles.",
  },
] as const;

/** Devuelve los `value` de un catálogo, tipados, para armar el enum de Zod. */
export function valuesOf<T extends string>(options: readonly Choice<T>[]): [T, ...T[]] {
  return options.map((o) => o.value) as [T, ...T[]];
}

/**
 * Etiqueta visible de una opción, buscada por su `value`.
 *
 * Existe para que los tests que sólo necesitan LLEGAR a un control (tildar una
 * opción y seguir verificando el payload) no hardcodeen el texto: un copy pass
 * como el de 2026-08-18 rompió 46 tests que buscaban por rótulo. Lo que sí es
 * contenido a verificar —un CTA, un encabezado— sigue asertándose literal.
 *
 * Tira si el `value` no existe en vez de devolver `undefined`: un test que
 * busca un rótulo inexistente tiene que romperse ahí, no arrastrar un
 * `undefined` hasta un selector que falla por otra razón.
 */
export function labelOf<T extends string>(options: readonly Choice<T>[], value: T): string {
  const found = options.find((o) => o.value === value);
  if (!found) {
    throw new Error(`labelOf: no existe la opción ${JSON.stringify(value)} en el catálogo.`);
  }
  return found.label;
}

/** `true` si el string es un plan válido — para leer `?plan=` sin inventar. */
export function isRequestedPlan(value: string | null | undefined): value is RequestedPlan {
  return REQUESTED_PLAN_OPTIONS.some((o) => o.value === value);
}

/**
 * Nombre corto de cada campo del formulario, en el ORDEN en el que aparece en
 * pantalla. Alimenta el resumen de "esto te falta" y define cuál es el primer
 * campo faltante al que se lleva el foco. El orden importa: mandar al usuario a
 * un campo del final cuando le falta uno del principio lo hace scrollear dos
 * veces.
 */
export const ACCESS_REQUEST_FIELD_LABELS: readonly (readonly [string, string])[] = [
  ["full_name", "Nombre y apellido"],
  ["email", "Email de trabajo"],
  ["phone", "WhatsApp"],
  ["business_name", "Nombre del negocio"],
  ["requested_vertical", "Rubro"],
  ["vertical_other_text", "De qué es tu negocio"],
  ["years_operating", "¿Hace cuánto está en actividad?"],
  ["staff_size", "¿Cuántas personas trabajan hoy?"],
  ["main_concern", "¿Qué necesitás entender primero?"],
  ["monthly_revenue_band", "Facturación mensual aproximada"],
  ["records_format", "¿Dónde registrás hoy ventas y gastos?"],
  ["history_depth", "¿Cuánto historial conservás?"],
  ["can_share_files", "¿Podrías compartir esos registros?"],
  ["requested_plan", "¿Cómo te gustaría empezar?"],
  ["consent", "Consentimiento"],
] as const;

/** Etiqueta corta de un campo; el nombre crudo si no está catalogado. */
export function accessRequestFieldLabel(field: string): string {
  return ACCESS_REQUEST_FIELD_LABELS.find(([k]) => k === field)?.[1] ?? field;
}

/**
 * Placeholder del textarea "contanos cómo lo llevás". El ejemplo concreto es lo
 * que le muestra al usuario qué nivel de detalle esperamos.
 */
export const RECORDS_NOTES_PLACEHOLDER =
  "Ej: anoto las ventas en un cuaderno y paso los totales a un Excel los domingos; " +
  "las compras las tengo en los remitos de los proveedores.";
