/**
 * Un número escrito a la argentina → number, o `null` si no se entiende.
 *
 * «1.000» es mil, no uno: el punto es separador de miles cuando hay coma
 * decimal («1.500,50») o cuando el patrón es de miles (`\d{1,3}(\.\d{3})+`).
 * Sin coma y con otro patrón («12.5») el punto se toma como decimal.
 */
export function parseNumeroAr(texto: string): number | null {
  const limpio = texto.trim().replace(/\s/g, "").replace(/^\$/, "");
  if (limpio === "") return null;
  let normal: string;
  if (limpio.includes(",")) {
    if (!/^\d{1,3}(\.\d{3})*(,\d+)?$|^\d+(,\d+)?$/.test(limpio)) return null;
    normal = limpio.replace(/\./g, "").replace(",", ".");
  } else if (/^\d{1,3}(\.\d{3})+$/.test(limpio)) {
    normal = limpio.replace(/\./g, "");
  } else if (/^\d+(\.\d+)?$/.test(limpio)) {
    normal = limpio;
  } else {
    return null;
  }
  const n = Number(normal);
  return Number.isFinite(n) ? n : null;
}
