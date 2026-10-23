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

/**
 * Valor del stock: unidades base × costo por unidad de venta / factor.
 *
 * Espejo de `sale_unit.valor_de_stock`. Sin la división, 5 kg (5000 g) de un
 * producto que cuesta $800 el kilo valían $4.000.000 en vez de $4.000. Un
 * factor ausente o no positivo vale 1: es lo que tienen todos los productos
 * por unidad.
 */
export function valorDeStock(
  unidadesBase: number,
  factor: number | null | undefined,
  costoPorUnidadDeVenta: number,
): number {
  const f = factor && factor > 0 ? factor : 1;
  return Math.round((unidadesBase * costoPorUnidadDeVenta * 100) / f) / 100;
}

/** ¿Se carga en kg o litros? (factor con nombre mayor a 1). */
export function esPorPeso(factor: number | null | undefined): boolean {
  return (factor ?? 1) > 1;
}

/** Cuántos decimales admite la unidad de venta: 1000 → 3 (gramos dentro del kg). */
function decimalesDe(factor: number): number {
  return factor > 1 ? String(factor).length - 1 : 0;
}

/**
 * Lo que tipea una persona en la unidad de VENTA ("0,750" kg) → unidades base
 * (750 g). Acepta coma o punto. `null` si no es una cantidad positiva o trae
 * más decimales de los que la unidad base puede contar (0,0005 kg no son
 * gramos enteros).
 */
export function aUnidadesBase(texto: string, factor: number | null | undefined): number | null {
  const f = factor && factor > 0 ? factor : 1;
  const limpio = texto.trim().replace(",", ".");
  const decimales = decimalesDe(f);
  const patron = decimales > 0 ? new RegExp(`^\\d+(\\.\\d{1,${decimales}})?$`) : /^\d+$/;
  if (!patron.test(limpio)) return null;
  // Se cuenta en enteros para no arrastrar el error de los floats (0,1 × 1000).
  const [entera, frac = ""] = limpio.split(".");
  const base = Number(entera) * f + Number((frac + "000000").slice(0, decimales) || "0") * (f / 10 ** decimales);
  return base > 0 ? base : null;
}

/** Unidades base → texto para un campo en la unidad de venta: 750 g → "0,75". */
export function enUnidadDeVenta(unidadesBase: number, factor: number | null | undefined): string {
  const f = factor && factor > 0 ? factor : 1;
  if (f === 1) return String(unidadesBase);
  return (unidadesBase / f).toLocaleString("es-AR", {
    minimumFractionDigits: 0,
    maximumFractionDigits: decimalesDe(f),
    useGrouping: false,
  });
}
