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

/** Valida el formato XX-XXXXXXXX-X (con o sin guiones). Suave: solo cuando hay valor. */
export function isValidCuil(value: string): boolean {
  return /^\d{2}-?\d{8}-?\d$/.test(value.trim());
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
