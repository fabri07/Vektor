/**
 * El puerto de escaneo de la caja sin red, contra el corpus COMPARTIDO.
 *
 * El mismo archivo lo lee `backend/app/tests/domain/test_scan_corpus.py`. Si
 * las dos implementaciones se separan, la caja offline puede cobrar un producto
 * distinto del que el servidor habría resuelto — y eso no se nota hasta el
 * arqueo. Esta compuerta es lo único que lo impide.
 */
import fs from "node:fs";
import path from "node:path";

import {
  buscarEnCatalogo,
  clasificar,
  normalizarBarcode,
  normalizarSku,
  variantesGtin,
} from "@/lib/pos/scanCodeLocal";

interface Caso {
  crudo: string;
  tipo: string;
  valor: string;
  aprendible: boolean;
  variantes: string[];
  por_que: string;
}

const CORPUS = path.resolve(__dirname, "../../../../../shared/pos/scan_corpus.json");
const casos: Caso[] = JSON.parse(fs.readFileSync(CORPUS, "utf-8")).casos;

describe("el corpus compartido", () => {
  it("existe donde lo busca el backend", () => {
    expect(fs.existsSync(CORPUS)).toBe(true);
    expect(casos.length).toBeGreaterThan(0);
  });

  it.each(casos.map((c) => [c.crudo || "(vacío)", c] as const))(
    "clasifica %s como dice el corpus",
    (_nombre, caso) => {
      const r = clasificar(caso.crudo);
      expect({ tipo: r.tipo, valor: r.valor, aprendible: r.aprendible }).toEqual({
        tipo: caso.tipo,
        valor: caso.valor,
        aprendible: caso.aprendible,
      });
      if (r.tipo === "GTIN" || r.tipo === "SCALE") {
        expect(variantesGtin(r.valor)).toEqual(caso.variantes);
      } else {
        expect(caso.variantes).toEqual([]);
      }
    },
  );
});

describe("buscarEnCatalogo", () => {
  const yerba = { id: "a", internal_sku: "VKT-ABC123DEF456", sku: "YER-01", barcode: "7790895000997" };
  const fideos = { id: "b", internal_sku: "VKT-ZZZ999ZZZ999", sku: "7790895000997", barcode: null };

  it("un GTIN guardado con 13 dígitos se encuentra escaneando el UPC-A de 12", () => {
    // El mismo número: un UPC-A es el EAN-13 con un 0 adelante. Sin las
    // variantes, el producto del proveedor (guardado con 13) no aparecería.
    const upc = { id: "c", internal_sku: null, sku: null, barcode: "0123456789012" };
    const hallados = buscarEnCatalogo(clasificar("123456789012"), [upc]);
    expect(hallados.map((h) => h.producto.id)).toEqual(["c"]);
    expect(hallados[0]!.matched_by).toBe("barcode");
  });

  it("el GTIN de uno que es el SKU opaco de otro sale AMBIGUO, no elige", () => {
    // El caso canónico: sin la búsqueda por SKU, la caja cobraría el primero
    // que aparece y nadie se enteraría.
    const hallados = buscarEnCatalogo(clasificar("7790895000997"), [yerba, fideos]);
    expect(hallados.map((h) => h.producto.id).sort()).toEqual(["a", "b"]);
  });

  it("un alfanumérico no se reduce a sus dígitos", () => {
    // `AB-96385074` no puede resolver al producto cuyo EAN-8 es 96385074.
    const ean8 = { id: "d", internal_sku: null, sku: null, barcode: "96385074" };
    expect(buscarEnCatalogo(clasificar("AB-96385074"), [ean8])).toEqual([]);
  });

  it("el código interno resuelve por su columna, sin importar mayúsculas", () => {
    const hallados = buscarEnCatalogo(clasificar("vkt-abc123def456"), [yerba, fideos]);
    expect(hallados.map((h) => h.producto.id)).toEqual(["a"]);
    expect(hallados[0]!.matched_by).toBe("internal_sku");
  });

  it("un código vacío no devuelve nada", () => {
    expect(buscarEnCatalogo(clasificar("   "), [yerba, fideos])).toEqual([]);
  });
});

describe("normalización", () => {
  it("un barcode es sólo dígitos y de largo plausible", () => {
    expect(normalizarBarcode("779-089-500-0997")).toBe("7790895000997");
    // `producto-123` no es un código de barras: 3 dígitos no alcanzan.
    expect(normalizarBarcode("producto-123")).toBeNull();
    expect(normalizarBarcode(null)).toBeNull();
  });

  it("un sku ignora tildes, mayúsculas y espacios de más", () => {
    expect(normalizarSku("  Ñandú  ROJO ")).toBe("nandu rojo");
    expect(normalizarSku("")).toBeNull();
  });
});
