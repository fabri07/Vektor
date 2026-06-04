// Etiquetas de métodos de pago en español rioplatense. Fuente única reusada por
// ventas, gastos y el arqueo de caja.

export const PAYMENT_LABELS: Record<string, string> = {
  cash: "Efectivo",
  debit_card: "Débito",
  credit_card: "Crédito",
  transfer: "Transferencia",
  qr: "QR",
  inflow: "Cobro cta. cte.",
  other: "Otro",
  OTROS: "Otros",
};

export function paymentLabel(method: string): string {
  return PAYMENT_LABELS[method] ?? method;
}
