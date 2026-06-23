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

// Validación de CUIT/CUIL y normalización de WhatsApp viven en `lib/fiscal.ts`
// (compartidas con Clientes). Se re-exportan para no romper imports existentes.
export { isValidCuil, isValidCuit, whatsappDigits } from "@/lib/fiscal";
