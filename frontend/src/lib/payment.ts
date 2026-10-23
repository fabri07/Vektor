// Etiquetas de métodos de pago en español rioplatense. Fuente única reusada por
// ventas, gastos y el arqueo de caja.

export const PAYMENT_LABELS: Record<string, string> = {
  cash: "Efectivo",
  debit_card: "Débito",
  credit_card: "Crédito",
  transfer: "Transferencia",
  qr: "QR",
  account: "Cuenta corriente",
  inflow: "Cobro cta. cte.",
  other: "Otro",
  OTROS: "Otros",
};

/**
 * Valores que el servidor ESCRIBE pero que nadie puede elegir como medio de
 * pago. `mixed` marca las ventas de un ticket de caja pagado con varios medios
 * (B9): el reparto real está en los pagos del ticket. Van aparte de
 * `PAYMENT_LABELS` a propósito — hay formularios que arman sus opciones con
 * ese mapa, y ofrecer «Mixto» ahí sería ofrecer un valor que el servidor
 * rechaza.
 */
export const DISPLAY_ONLY_PAYMENT_LABELS: Record<string, string> = {
  mixed: "Mixto",
};

/** Etiqueta para MOSTRAR un medio de pago. Nunca para armar opciones. */
export function paymentLabel(method: string): string {
  return PAYMENT_LABELS[method] ?? DISPLAY_ONLY_PAYMENT_LABELS[method] ?? method;
}
