/**
 * B·5b: qué es el importe de una compra respecto del IVA, y qué se hace con el
 * envío. Es la misma regla que el backend (`domain/comprobante_de_compra.py`):
 * nunca un 21 % por defecto, y un envío con monto tiene que decir a dónde va.
 */
export type TaxTreatment = "desconocido" | "incluido" | "adicional" | "no_aplica";
export type ShippingMode = "desconocido" | "repartir" | "gasto_aparte" | "no_hubo";

export interface PurchaseTerms {
  taxTreatment: TaxTreatment;
  /** En porcentaje, como lo tipea el usuario ("21", "10,5"). */
  taxRatePct: string;
  shippingAmount: string;
  shippingMode: ShippingMode;
}

export const EMPTY_TERMS: PurchaseTerms = {
  taxTreatment: "desconocido",
  taxRatePct: "",
  shippingAmount: "",
  shippingMode: "desconocido",
};

export const TAX_LABELS: Record<TaxTreatment, string> = {
  desconocido: "No sé",
  incluido: "El importe ya incluye IVA",
  adicional: "El IVA se pagó aparte",
  no_aplica: "No lleva IVA",
};

export const SHIPPING_LABELS: Record<ShippingMode, string> = {
  desconocido: "No sé / no hubo dato",
  repartir: "Repartirlo en el costo de los artículos",
  gasto_aparte: "Cargarlo como gasto de envío aparte",
  no_hubo: "No hubo envío",
};

function num(raw: string): number {
  return Number(raw.replace(",", "."));
}

/** El motivo por el que los datos no se pueden mandar, o null si están bien. */
export function termsError(t: PurchaseTerms): string | null {
  const conAlicuota = t.taxTreatment === "incluido" || t.taxTreatment === "adicional";
  if (conAlicuota) {
    const r = num(t.taxRatePct);
    // Mismos límites que el backend (fracción > 0 y < 1, con 4 decimales):
    // 0,01 % a 99,99 %. Fuera de eso el redondeo daría 0 o 1 y un 422.
    if (!t.taxRatePct || !Number.isFinite(r) || r < 0.01 || r > 99.99) {
      return "Indicá la alícuota del IVA entre 0,01 y 99,99 (por ejemplo 21).";
    }
  }
  const monto = t.shippingAmount ? num(t.shippingAmount) : 0;
  if (!Number.isFinite(monto) || monto < 0) return "El envío no puede ser negativo.";
  if ((t.shippingMode === "repartir" || t.shippingMode === "gasto_aparte") && monto <= 0) {
    return "Indicá cuánto costó el envío.";
  }
  if ((t.shippingMode === "desconocido" || t.shippingMode === "no_hubo") && monto > 0) {
    return "Indicá si el envío entra al costo de los artículos o va como gasto aparte.";
  }
  return null;
}

/** Los campos opcionales del pedido (`shipping`, `tax`). Sin dato no se manda nada. */
export function termsPayload(t: PurchaseTerms): {
  shipping?: { amount: number; mode: ShippingMode };
  tax?: { treatment: TaxTreatment; rate?: number };
} {
  const out: ReturnType<typeof termsPayload> = {};
  const monto = t.shippingAmount ? num(t.shippingAmount) : 0;
  if (t.shippingMode !== "desconocido" || monto > 0) {
    out.shipping = { amount: monto, mode: t.shippingMode };
  }
  if (t.taxTreatment !== "desconocido") {
    const conAlicuota = t.taxTreatment === "incluido" || t.taxTreatment === "adicional";
    out.tax = {
      treatment: t.taxTreatment,
      ...(conAlicuota ? { rate: Number((num(t.taxRatePct) / 100).toFixed(4)) } : {}),
    };
  }
  return out;
}
