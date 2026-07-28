/**
 * Compuerta de contraste sobre los tokens que llevan texto encima.
 *
 * Mide de verdad (fórmula WCAG 2.1 sobre los valores de `tailwind.config.ts`)
 * en vez de confiar en que alguien miró la pantalla. La review que originó
 * esto encontró un CTA a 2,11:1 y un hint de campo financiero a 2,54:1, los
 * dos invisibles para el detector automático y para la vista entrenada.
 *
 * Lo que este test NO hace: mirar el DOM. Si alguien vuelve a escribir
 * `text-gray-400` sobre blanco en un componente, esto no lo ve. Fija los
 * tokens, que es donde la decisión queda tomada una sola vez.
 */

/** Canal lineal, según WCAG 2.1 (relative luminance). */
function canalLineal(valor: number): number {
  const s = valor / 255;
  return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
}

function luminancia([r, g, b]: readonly [number, number, number]): number {
  return 0.2126 * canalLineal(r) + 0.7152 * canalLineal(g) + 0.0722 * canalLineal(b);
}

function contraste(
  a: readonly [number, number, number],
  b: readonly [number, number, number],
): number {
  const [claro, oscuro] = [luminancia(a), luminancia(b)].sort((x, y) => y - x);
  return (claro! + 0.05) / (oscuro! + 0.05);
}

type RGB = readonly [number, number, number];

const BLANCO: RGB = [255, 255, 255];
// Valores tomados de `tailwind.config.ts` (`vkColors`), que es la fuente de
// verdad de las clases utilitarias. Ojo: `globals.css` declara varios de estos
// mismos nombres con valores INVERTIDOS, pero son inertes para las utilidades.
const VEKTOR_BLUE_STRONG: RGB = [37, 99, 235];
const VEKTOR_TEAL_DEEP: RGB = [23, 119, 110];
const VEKTOR_TEAL: RGB = [39, 199, 184];
const VEKTOR_MUTED: RGB = [144, 162, 188];
const VEKTOR_NIGHT: RGB = [5, 9, 19];
const VK_TEXT_MUTED: RGB = [100, 116, 139];

/** Mínimo AA para texto normal. El CTA es `text-sm font-semibold` (14px/600), */
/** que NO califica como "large text" (eso pide ≥18,66px en negrita). */
const AA_TEXTO_NORMAL = 4.5;

describe("contraste de los tokens con texto encima", () => {
  test("el gradiente del CTA principal pasa AA en TODO su recorrido", () => {
    // El peor punto de un gradiente no tiene por qué ser un extremo: hay que
    // barrerlo. Se muestrea cada 1 % entre las dos paradas.
    let peor = Infinity;
    for (let t = 0; t <= 100; t++) {
      const punto = VEKTOR_BLUE_STRONG.map((v, i) =>
        Math.round(v + (VEKTOR_TEAL_DEEP[i]! - v) * (t / 100)),
      ) as unknown as RGB;
      peor = Math.min(peor, contraste(BLANCO, punto));
    }
    expect(peor).toBeGreaterThanOrEqual(AA_TEXTO_NORMAL);
  });

  test("el teal de marca sigue siendo inutilizable para texto blanco", () => {
    // No es un token "roto": es un color de marca que funciona para superficies
    // e iconos y no para texto. Este test lo deja escrito, para que el próximo
    // que quiera usarlo en un botón se entere acá y no en una auditoría.
    expect(contraste(BLANCO, VEKTOR_TEAL)).toBeLessThan(AA_TEXTO_NORMAL);
  });

  test("el texto atenuado sobre blanco pasa AA", () => {
    // `vk-text-muted` reemplazó a `text-gray-400` (#9ca3af, 2,54:1) en el
    // onboarding. Está a 4,76:1: pasa, pero con apenas 0,26 de margen —
    // aclararlo aunque sea un poco rompe cuatro pantallas de una.
    expect(contraste(VK_TEXT_MUTED, BLANCO)).toBeGreaterThanOrEqual(AA_TEXTO_NORMAL);
  });

  test("el gris de Tailwind que se sacó del onboarding no habría pasado", () => {
    const GRAY_400: RGB = [156, 163, 175];
    expect(contraste(GRAY_400, BLANCO)).toBeLessThan(AA_TEXTO_NORMAL);
  });

  test("el texto atenuado del embudo oscuro pasa AA con holgura", () => {
    expect(contraste(VEKTOR_MUTED, VEKTOR_NIGHT)).toBeGreaterThanOrEqual(AA_TEXTO_NORMAL);
  });
});
