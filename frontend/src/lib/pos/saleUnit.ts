/**
 * Cómo se le muestra a un humano una cantidad contada en unidades base.
 *
 * Puerto de `backend/app/domain/sale_unit.py` para el ticket que la caja
 * imprime SIN RED: ahí el recibo lo arma el cliente y no el servidor, y una
 * cantidad mal formateada hace que el comprobante no explique su importe.
 * Presentación pura: nada que haga cuentas debe llamarlo.
 */

const ETIQUETAS: Record<string, string> = {
  "unit:1": "un.",
  "gram:1": "g",
  "gram:1000": "kg",
  "milliliter:1": "ml",
  "milliliter:1000": "L",
};

const BASE: Record<string, string> = { unit: "un.", gram: "g", milliliter: "ml" };

export function etiquetaDeUnidad(saleUnit: string, factor: number): string {
  return ETIQUETAS[`${saleUnit}:${factor}`] ?? BASE[saleUnit] ?? "un.";
}

/** Número en convención argentina: '.' para miles y ',' para decimales. */
function formatoAr(valor: number, decimales: number): string {
  return valor.toLocaleString("es-AR", {
    minimumFractionDigits: decimales,
    maximumFractionDigits: decimales,
  });
}

/**
 * Unidades base → texto con su unidad. 750 g con factor 1000 → `0,750 kg`.
 *
 * Los decimales salen del factor y no se recortan: al comparar un ticket con el
 * stock, la cantidad de decimales es lo que deja ver si el redondeo se comió
 * algo. Un factor sin nombre (500 no es "medio kilo") se muestra en unidad
 * base: bautizarlo sería inventar una unidad que el negocio no usa.
 */
export function formatearCantidad(
  unidadesBase: number,
  saleUnit: string,
  factor: number,
): string {
  const etiqueta = etiquetaDeUnidad(saleUnit, factor);
  const conocida = ETIQUETAS[`${saleUnit}:${factor}`] !== undefined;
  if (!conocida || factor === 1) {
    return `${formatoAr(unidadesBase, 0)} ${etiqueta}`;
  }
  const decimales = String(factor).length - 1;
  return `${formatoAr(unidadesBase / factor, decimales)} ${etiqueta}`;
}
