/**
 * Régimen fiscal del negocio — set compartido entre onboarding y settings.
 *
 * Contrato acordado con backend: valor opcional dentro de
 *   { 'monotributo', 'responsable_inscripto', 'informal' }
 *
 * Nota: el valor legacy "registered" (binario previo) puede seguir llegando
 * desde algunos endpoints (p.ej. el preview de cierre de caja) hasta que el
 * backend termine de migrar. Usar `isFiscallyRegistered()` para lógica que solo
 * necesita distinguir "registrado vs no registrado" de forma robusta a ambos.
 */

export type FiscalCondition = "monotributo" | "responsable_inscripto" | "informal";

/** Valor legacy que algunos endpoints todavía pueden devolver. */
export type LegacyFiscalCondition = FiscalCondition | "registered";

export interface FiscalConditionOption {
  value: FiscalCondition;
  label: string;
  detail: string;
}

/**
 * Mapa label↔value canónico. Fuente de verdad única para onboarding y settings.
 * El orden define el orden de render.
 */
export const FISCAL_CONDITION_OPTIONS: readonly FiscalConditionOption[] = [
  {
    value: "monotributo",
    label: "Monotributo",
    detail: "Inscripto en el régimen simplificado ante ARCA (ex-AFIP).",
  },
  {
    value: "responsable_inscripto",
    label: "Responsable Inscripto",
    detail: "Inscripto en IVA y Ganancias ante ARCA (ex-AFIP).",
  },
  {
    value: "informal",
    label: "Informal / No registrado",
    detail: "El arqueo de caja es control interno, sin valor fiscal.",
  },
] as const;

/** Conjunto de valores válidos, para validar/normalizar entradas. */
const VALID_VALUES = new Set<string>(FISCAL_CONDITION_OPTIONS.map((o) => o.value));

/** True si el string es un `FiscalCondition` válido del contrato nuevo. */
export function isFiscalCondition(value: string | null | undefined): value is FiscalCondition {
  return value != null && VALID_VALUES.has(value);
}

/**
 * True si el negocio está registrado fiscalmente (monotributo o RI), tolerando
 * el valor legacy "registered". `informal` y `null` devuelven false.
 */
export function isFiscallyRegistered(
  value: LegacyFiscalCondition | null | undefined,
): boolean {
  return value === "monotributo" || value === "responsable_inscripto" || value === "registered";
}

/** Etiqueta legible para un valor; cadena vacía si es null. */
export function fiscalConditionLabel(value: FiscalCondition | null | undefined): string {
  return FISCAL_CONDITION_OPTIONS.find((o) => o.value === value)?.label ?? "";
}

/**
 * Nota informativa de privacidad mostrada junto al selector de régimen fiscal.
 * Referencias legales: Ley 25.326 (Protección de Datos Personales, Argentina),
 * AAIP como autoridad de aplicación, LGPD (Brasil) como referencia regional.
 * Redacción intencionalmente sin promesas de certificación que no podemos garantizar.
 */
export const FISCAL_PRIVACY_NOTE =
  "Te lo preguntamos solo para interpretar mejor tu información —impuestos, " +
  "márgenes y salud financiera—; es opcional, no bloquea ninguna función y lo " +
  "podés cambiar cuando quieras. Tus datos son tuyos: no los difundimos, no los " +
  "vendemos ni los compartimos con terceros. Cuidamos tu información siguiendo la " +
  "Ley 25.326 de Protección de Datos Personales de Argentina —cuya autoridad de " +
  "aplicación es la AAIP— y las normativas de protección de datos de la región.";
