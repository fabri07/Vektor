import {
  Code128Error,
  _PATTERNS_FOR_TEST,
  code128Svg,
  code128Values,
  code128Widths,
} from "@/lib/barcode/code128";

/**
 * Referencias generadas con una implementación independiente (reportlab 4.2.5,
 * `Code128(v).decompose()`), sobre textos que esa implementación codifica
 * enteros en el juego B — los mismos símbolos que produce la nuestra.
 */
const REFERENCIA: [string, number[], string][] = [
  ["A", [104, 33, 34, 106], "2112141113231311232331112"],
  [
    "Hola 1",
    [104, 40, 79, 76, 65, 0, 17, 68, 106],
    "2112142311131341112211141211242122221232211412212331112",
  ],
  [
    "VKT-abc~",
    [104, 54, 43, 52, 13, 65, 66, 67, 94, 25, 106],
    "2112143111231123312133111221321211241214211411221311413211222331112",
  ],
];

describe("code128", () => {
  it.each(REFERENCIA)("%s coincide con la referencia (valores y verificador)", (texto, valores, anchos) => {
    expect(code128Values(texto)).toEqual(valores);
    expect(code128Widths(texto)).toBe(anchos);
  });

  it("verificador calculado a mano: start 104 + Σ valor × posición, mod 103", () => {
    // "VKT-0123456789AB": V=54, K=43, T=52, -=13, dígitos 16..25, A=33, B=34.
    const datos = [54, 43, 52, 13, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 33, 34];
    const esperado = datos.reduce((acc, v, i) => acc + v * (i + 1), 104) % 103;
    const valores = code128Values("VKT-0123456789AB");
    expect(valores.slice(1, -2)).toEqual(datos);
    expect(valores.at(-2)).toBe(esperado);
  });

  it("la tabla es coherente: 107 símbolos distintos de 11 módulos (stop 13)", () => {
    expect(_PATTERNS_FOR_TEST).toHaveLength(107);
    expect(new Set(_PATTERNS_FOR_TEST).size).toBe(107);
    _PATTERNS_FOR_TEST.forEach((p, i) => {
      const suma = [...p].reduce((a, c) => a + Number(c), 0);
      expect(suma).toBe(i === 106 ? 13 : 11);
    });
  });

  it("rechaza lo que el juego B no tiene", () => {
    expect(() => code128Values("")).toThrow(Code128Error);
    expect(() => code128Values("año")).toThrow(Code128Error);
  });

  it("el SVG dibuja una barra por cada ancho impar y deja zona silenciosa", () => {
    const svg = code128Svg("A");
    // 4 símbolos: 3 de 3 barras + stop de 4 barras.
    expect(svg.match(/<rect/g)).toHaveLength(13);
    expect(svg).toContain('x="10"');
  });
});
