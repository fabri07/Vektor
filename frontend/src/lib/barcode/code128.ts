/**
 * Code 128, juego B, sin dependencias (B12).
 *
 * Alcanza para lo que imprime Véktor: el `internal_sku` (`VKT-…`) de productos
 * sin código de fábrica. El juego B cubre ASCII 32–126; no se usa el juego C
 * (pares de dígitos) porque el ahorro de ancho no justifica el cambio de juego
 * en códigos de 16 caracteres, y un encoder más simple es uno menos que
 * verificar.
 *
 * Cada símbolo son 6 anchos alternando barra/espacio (11 módulos); el stop son
 * 7 (13 módulos). El verificador es mod 103: start + Σ valor × posición.
 */

// Anchos de los símbolos 0–106 (tabla estándar de Code 128).
const PATTERNS: readonly string[] = [
  "212222", "222122", "222221", "121223", "121322", "131222", "122213", "122312",
  "132212", "221213", "221312", "231212", "112232", "122132", "122231", "113222",
  "123122", "123221", "223211", "221132", "221231", "213212", "223112", "312131",
  "311222", "321122", "321221", "312212", "322112", "322211", "212123", "212321",
  "232121", "111323", "131123", "131321", "112313", "132113", "132311", "211313",
  "231113", "231311", "112133", "112331", "132131", "113123", "113321", "133121",
  "313121", "211331", "231131", "213113", "213311", "213131", "311123", "311321",
  "331121", "312113", "312311", "332111", "314111", "221411", "431111", "111224",
  "111422", "121124", "121421", "141122", "141221", "112214", "112412", "122114",
  "122411", "142112", "142211", "241211", "221114", "413111", "241112", "134111",
  "111242", "121142", "121241", "114212", "124112", "124211", "411212", "421112",
  "421211", "212141", "214121", "412121", "111143", "111341", "131141", "114113",
  "114311", "411113", "411311", "113141", "114131", "311141", "411131", "211412",
  "211214", "211232", "2331112",
];

const START_B = 104;
const STOP = 106;

export class Code128Error extends Error {}

/** Los valores de símbolo: start B, datos, verificador y stop. */
export function code128Values(text: string): number[] {
  if (text.length === 0) throw new Code128Error("No hay nada que codificar.");
  const datos = [...text].map((ch) => {
    const code = ch.charCodeAt(0);
    if (ch.length !== 1 || code < 32 || code > 126) {
      throw new Code128Error(`«${ch}» no se puede codificar en Code 128 B.`);
    }
    return code - 32;
  });
  const suma = datos.reduce((acc, v, i) => acc + v * (i + 1), START_B);
  return [START_B, ...datos, suma % 103, STOP];
}

/** Anchos de módulo, alternando barra y espacio, empezando por barra. */
export function code128Widths(text: string): string {
  return code128Values(text)
    .map((v) => PATTERNS[v] ?? "")
    .join("");
}

/**
 * SVG listo para imprimir. Deja zona silenciosa de 10 módulos a cada lado: sin
 * ella el lector no encuentra dónde empieza el código.
 */
export function code128Svg(
  text: string,
  { moduleWidth = 1, height = 40 }: { moduleWidth?: number; height?: number } = {},
): string {
  const anchos = code128Widths(text);
  const quiet = 10;
  let x = quiet;
  const barras: string[] = [];
  [...anchos].forEach((w, i) => {
    const ancho = Number(w);
    if (i % 2 === 0) barras.push(`<rect x="${x}" y="0" width="${ancho}" height="${height}"/>`);
    x += ancho;
  });
  const total = x + quiet;
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${total} ${height}" ` +
    `width="${total * moduleWidth}" height="${height}" preserveAspectRatio="none" ` +
    `shape-rendering="crispEdges" role="img" aria-label="${escapeXml(text)}">` +
    `<g fill="#000">${barras.join("")}</g></svg>`
  );
}

function escapeXml(s: string): string {
  return s.replace(/[<>&"']/g, (c) => `&#${c.charCodeAt(0)};`);
}

/** Sólo para los tests: la tabla tiene que ser coherente. */
export const _PATTERNS_FOR_TEST = PATTERNS;
