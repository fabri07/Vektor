// Helpers compartidos de la sección Proveedores. Reusados por la lista
// (`/suppliers`) y la página de detalle (`/suppliers/[id]`) para no duplicar
// el formateo de teléfono/forma de pago ni la validación de CUIL.

export const PAYMENT_METHOD_LABELS: Record<string, string> = {
  cash: "Efectivo",
  debit_card: "Tarjeta débito",
  credit_card: "Tarjeta crédito",
  transfer: "Transferencia",
  qr: "QR / Mercado Pago",
  account: "Cuenta corriente",
  other: "Otro",
};

export const PAYMENT_METHOD_OPTIONS = Object.entries(PAYMENT_METHOD_LABELS).map(
  ([value, label]) => ({ value, label }),
);

export function paymentLabel(method: string): string {
  return PAYMENT_METHOD_LABELS[method] ?? method;
}

// Pesos del dígito verificador del CUIL/CUIT (sobre los primeros 10 dígitos).
const CUIL_WEIGHTS = [5, 4, 3, 2, 7, 6, 5, 4, 3, 2];

/**
 * Valida un CUIL/CUIT argentino: formato XX-XXXXXXXX-X (guiones opcionales) Y el
 * dígito verificador (módulo 11). Así detecta un CUIL bien formateado pero mal
 * tipeado, no solo el formato. Suave: solo se llama cuando hay valor.
 */
export function isValidCuil(value: string): boolean {
  const trimmed = value.trim();
  if (!/^\d{2}-?\d{8}-?\d$/.test(trimmed)) return false;
  const nums = trimmed.replace(/-/g, "").split("").map(Number);
  if (nums.length !== 11) return false;
  let acc = 0;
  for (let i = 0; i < 10; i++) {
    acc += (nums[i] ?? 0) * (CUIL_WEIGHTS[i] ?? 0);
  }
  let verifier = 11 - (acc % 11);
  if (verifier === 11) verifier = 0;
  else if (verifier === 10) verifier = 9;
  return verifier === (nums[10] ?? -1);
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
