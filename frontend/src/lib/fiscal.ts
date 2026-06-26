// Helpers fiscales argentinos compartidos (Proveedores + Clientes). Centraliza la
// validación de CUIT/CUIL (módulo 11), DNI, normalización de WhatsApp y las
// etiquetas/opciones de condición frente al IVA y tipo de cliente. `lib/suppliers.ts`
// re-exporta lo que ya usaba para no romper imports existentes.

// Pesos del dígito verificador del CUIT/CUIL (sobre los primeros 10 dígitos).
const CUIT_WEIGHTS = [5, 4, 3, 2, 7, 6, 5, 4, 3, 2];

/**
 * Valida un CUIT/CUIL argentino: formato XX-XXXXXXXX-X (guiones opcionales) Y el
 * dígito verificador (módulo 11). Así detecta un número bien formateado pero mal
 * tipeado, no solo el formato. Suave: solo se llama cuando hay valor.
 */
export function isValidCuit(value: string): boolean {
  const trimmed = value.trim();
  if (!/^\d{2}-?\d{8}-?\d$/.test(trimmed)) return false;
  const nums = trimmed.replace(/-/g, "").split("").map(Number);
  if (nums.length !== 11) return false;
  let acc = 0;
  for (let i = 0; i < 10; i++) {
    acc += (nums[i] ?? 0) * (CUIT_WEIGHTS[i] ?? 0);
  }
  let verifier = 11 - (acc % 11);
  if (verifier === 11) verifier = 0;
  else if (verifier === 10) verifier = 9;
  return verifier === (nums[10] ?? -1);
}

/** Alias histórico: la sección Proveedores hablaba de "CUIL". Misma validación. */
export const isValidCuil = isValidCuit;

/** Valida un DNI argentino: 7 u 8 dígitos. Suave: solo se llama cuando hay valor. */
export function isValidDni(value: string): boolean {
  return /^\d{7,8}$/.test(value.trim());
}

/**
 * Normaliza un teléfono a dígitos para wa.me. wa.me requiere formato internacional
 * SIN "+". Si el número no trae código de país, se asume Argentina (54) — sin esto,
 * un teléfono local (ej. "11 1234-5678") generaría un link wa.me inválido.
 */
export function whatsappDigits(phone: string): string {
  const digits = phone.replace(/[^\d]/g, "");
  if (!digits) return "";
  return digits.startsWith("54") ? digits : `54${digits}`;
}

/**
 * ¿El teléfono tiene suficientes dígitos para armar un link wa.me plausible?
 *
 * No intenta distinguir móvil de fijo (la regla del "9" de celulares AR no se
 * puede inferir con certeza). Acota el largo a un rango plausible: un número AR
 * completo (54 + área + abonado) tiene ≥ 12 dígitos; aceptamos ≥ 11 para no
 * falsear teléfonos del interior cargados sin el 54, y ≤ 15 (máximo E.164) para
 * rechazar strings que no son teléfonos (CUIT, códigos internos pegados por error).
 * Si devuelve false, la UI deshabilita el botón y muestra una advertencia.
 */
export function isLikelyValidWhatsApp(phone: string | null | undefined): boolean {
  if (!phone) return false;
  const len = whatsappDigits(phone).length;
  return len >= 11 && len <= 15;
}

// ── Condición frente al IVA (canónica, espejo del backend) ─────────────────────

export const IVA_CONDITION_LABELS: Record<string, string> = {
  consumidor_final: "Consumidor final",
  monotributo: "Monotributo",
  responsable_inscripto: "Responsable inscripto",
  exento: "Exento",
};

export const IVA_CONDITION_OPTIONS = Object.entries(IVA_CONDITION_LABELS).map(
  ([value, label]) => ({ value, label }),
);

export function ivaConditionLabel(condition: string | null | undefined): string {
  if (!condition) return "—";
  return IVA_CONDITION_LABELS[condition] ?? condition;
}

// ── Tipo de cliente (persona / empresa) ────────────────────────────────────────

export const CUSTOMER_TYPE_LABELS: Record<string, string> = {
  person: "Persona",
  company: "Empresa",
};

export function customerTypeLabel(type: string | null | undefined): string {
  if (!type) return "—";
  return CUSTOMER_TYPE_LABELS[type] ?? type;
}

// ── Centinela de cliente ────────────────────────────────────────────────────────

/** True si es el cliente centinela "Local" (ventas sin cliente identificado).
 *  Se identifica SOLO por el flag `is_sentinel` del backend, nunca por el nombre. */
export function isSentinelCustomer(c: unknown): boolean {
  return (c as { is_sentinel?: boolean } | null)?.is_sentinel === true;
}
