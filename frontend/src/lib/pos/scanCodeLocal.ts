/**
 * Qué es un código escaneado, cuando la caja no puede preguntarle al servidor.
 *
 * Con red, `/products/lookup` clasifica y busca. Sin red hay que hacerlo acá, y
 * el riesgo de tener dos implementaciones es concreto: si esta fuera más laxa
 * que la del servidor, la caja cobraría un producto que el servidor habría
 * marcado como ambiguo. Por eso es un puerto literal de
 * `backend/app/domain/scan_code.py` + `ProductRepository.find_by_scan`, y los
 * dos lados se prueban contra el MISMO corpus (`shared/pos/scan_corpus.json`).
 *
 * Límite declarado: `normalizarSku` usa `toLowerCase()` y Python usa
 * `casefold()`. Difieren en caracteres como `ß` (casefold da `ss`), que no
 * aparecen en un SKU argentino. La diferencia sólo puede hacer que acá NO se
 * encuentre algo que el servidor sí encontraría — nunca al revés —, así que
 * falla del lado seguro: el cajero busca por nombre.
 */

export type TipoDeCodigo = "INTERNAL_SKU" | "GTIN" | "SCALE" | "OPAQUE";

export interface CodigoEscaneado {
  tipo: TipoDeCodigo;
  /** Limpio de bordes y, si es INTERNAL_SKU, en mayúsculas. */
  valor: string;
  /** Sólo un GTIN se puede vincular a un producto desde la caja. */
  aprendible: boolean;
}

/** Superconjunto a propósito: rechazar una `I` acá lo mandaría a OPAQUE. */
const INTERNAL_SKU = /^VKT-[A-Z0-9]{12}$/;

const LARGOS_GTIN = [8, 12, 13, 14];

/** Lo que agrega el lector al final o al principio. Nunca se toca el interior. */
const BORDES = new Set([" ", "\t", "\r", "\n", "\0"]);

function recortarBordes(s: string): string {
  let i = 0;
  let j = s.length;
  while (i < j && BORDES.has(s[i] as string)) i += 1;
  while (j > i && BORDES.has(s[j - 1] as string)) j -= 1;
  return s.slice(i, j);
}

/** Dígito verificador GS1 (módulo 10) de los dígitos SIN el verificador. */
export function digitoVerificadorGs1(cuerpo: string): number {
  let total = 0;
  for (let i = 0; i < cuerpo.length; i += 1) {
    const d = Number(cuerpo[cuerpo.length - 1 - i]);
    total += d * (i % 2 === 0 ? 3 : 1);
  }
  return (10 - (total % 10)) % 10;
}

export function gtinValido(digitos: string): boolean {
  if (!/^\d+$/.test(digitos) || !LARGOS_GTIN.includes(digitos.length)) return false;
  return digitoVerificadorGs1(digitos.slice(0, -1)) === Number(digitos.slice(-1));
}

export function clasificar(crudo: string): CodigoEscaneado {
  const limpio = recortarBordes(crudo);
  const mayus = limpio.toUpperCase();
  if (INTERNAL_SKU.test(mayus)) {
    return { tipo: "INTERNAL_SKU", valor: mayus, aprendible: false };
  }
  if (gtinValido(limpio)) {
    // EAN-13 con prefijo 2: lo genera la balanza del local y lleva el peso
    // adentro. No se aprende: ataría el producto a UN peso.
    if (limpio.length === 13 && limpio[0] === "2") {
      return { tipo: "SCALE", valor: limpio, aprendible: false };
    }
    return { tipo: "GTIN", valor: limpio, aprendible: true };
  }
  return { tipo: "OPAQUE", valor: limpio, aprendible: false };
}

/**
 * Las formas en que el MISMO GTIN puede estar guardado.
 *
 * Un UPC-A de 12 dígitos es el EAN-13 con un `0` adelante. Sólo aplica a GTIN:
 * en un código interno los ceros a la izquierda son significativos.
 */
export function variantesGtin(digitos: string): string[] {
  const base = digitos.replace(/^0+/, "");
  const formas = new Set<string>([digitos]);
  for (const largo of LARGOS_GTIN) {
    if (base.length <= largo) formas.add(base.padStart(largo, "0"));
  }
  return [...formas].filter((f) => LARGOS_GTIN.includes(f.length)).sort();
}

/** Sólo dígitos, y sólo si quedan entre 8 y 14: si no, no es un barcode. */
export function normalizarBarcode(s: string | null | undefined): string | null {
  if (!s) return null;
  const digitos = s.replace(/\D/g, "");
  if (digitos.length < 8 || digitos.length > 14) return null;
  return digitos;
}

/** NFKD + sin diacríticos + minúsculas + espacios colapsados. */
export function normalizarSku(s: string | null | undefined): string | null {
  if (!s) return null;
  const limpio = s
    .normalize("NFKD")
    .replace(/\p{M}/gu, "")
    .toLowerCase()
    .trim()
    .split(/\s+/)
    .join(" ");
  return limpio || null;
}

export interface ProductoBuscable {
  id: string;
  internal_sku?: string | null;
  sku?: string | null;
  barcode?: string | null;
}

export interface Coincidencia<T extends ProductoBuscable> {
  producto: T;
  /** Por qué columna coincidió: `internal_sku` | `barcode` | `sku`. */
  matched_by: string;
}

/**
 * Los productos del catálogo local que responden a un código. NO elige.
 *
 * Espejo de `ProductRepository.find_by_scan`: cada tipo busca en SU columna
 * **y además** todos se comparan contra el SKU como texto literal. Esa segunda
 * búsqueda es la que deja ver el caso ambiguo —el GTIN de A es el SKU opaco de
 * B—; sin ella la caja elegiría el primero que aparece y nadie se enteraría.
 */
export function buscarEnCatalogo<T extends ProductoBuscable>(
  codigo: CodigoEscaneado,
  productos: readonly T[],
): Coincidencia<T>[] {
  if (!codigo.valor) return [];
  const encontrados = new Map<string, Coincidencia<T>>();

  if (codigo.tipo === "INTERNAL_SKU") {
    for (const p of productos) {
      if ((p.internal_sku ?? "").toUpperCase() === codigo.valor) {
        if (!encontrados.has(p.id)) {
          encontrados.set(p.id, { producto: p, matched_by: "internal_sku" });
        }
      }
    }
  } else if (codigo.tipo === "GTIN" || codigo.tipo === "SCALE") {
    const variantes = new Set(variantesGtin(codigo.valor));
    for (const p of productos) {
      const normalizado = normalizarBarcode(p.barcode);
      if (normalizado && variantes.has(normalizado) && !encontrados.has(p.id)) {
        encontrados.set(p.id, { producto: p, matched_by: "barcode" });
      }
    }
  }

  const sku = normalizarSku(codigo.valor);
  if (sku) {
    for (const p of productos) {
      if (normalizarSku(p.sku) === sku && !encontrados.has(p.id)) {
        encontrados.set(p.id, { producto: p, matched_by: "sku" });
      }
    }
  }
  return [...encontrados.values()];
}
