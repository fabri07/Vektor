/**
 * Precio y costo son por unidad de VENTA (kg, L) y el stock está en unidades
 * BASE (g, ml): toda cuenta stock × costo divide por el factor. Mismos casos
 * que `backend/app/tests/domain/test_sale_unit.py`.
 */
import {
  aUnidadesBase,
  enUnidadDeVenta,
  formatearCantidad,
  valorDeStock,
} from "@/lib/pos/saleUnit";

describe("valorDeStock", () => {
  it("5 kg (5000 g) a $800 el kilo valen $4.000, no $4.000.000", () => {
    expect(valorDeStock(5000, 1000, 800)).toBe(4000);
  });

  it("por unidad, el factor es 1", () => {
    expect(valorDeStock(12, 1, 150.5)).toBe(1806);
  });

  it("un factor ausente o no positivo vale 1 (productos anteriores a B3)", () => {
    expect(valorDeStock(3, undefined, 10)).toBe(30);
    expect(valorDeStock(3, 0, 10)).toBe(30);
  });

  it("redondea a centavos", () => {
    expect(valorDeStock(750, 1000, 1234.56)).toBe(925.92);
  });
});

describe("formatearCantidad", () => {
  it("750 g con factor 1000 → 0,750 kg", () => {
    expect(formatearCantidad(750, "gram", 1000)).toBe("0,750 kg");
  });
});

describe("aUnidadesBase: lo que se tipea en kg o L → unidades base", () => {
  it("0,750 kg son 750 g, con coma o con punto", () => {
    expect(aUnidadesBase("0,750", 1000)).toBe(750);
    expect(aUnidadesBase("1.5", 1000)).toBe(1500);
    expect(aUnidadesBase("2", 1000)).toBe(2000);
  });

  it("sin errores de float: 0,1 kg son 100 g exactos", () => {
    expect(aUnidadesBase("0,1", 1000)).toBe(100);
    expect(aUnidadesBase("2,345", 1000)).toBe(2345);
  });

  it("rechaza lo que la unidad base no puede contar, y lo que no es positivo", () => {
    expect(aUnidadesBase("0,0005", 1000)).toBeNull();
    expect(aUnidadesBase("0", 1000)).toBeNull();
    expect(aUnidadesBase("abc", 1000)).toBeNull();
  });

  it("por unidad, sólo enteros", () => {
    expect(aUnidadesBase("3", 1)).toBe(3);
    expect(aUnidadesBase("1,5", 1)).toBeNull();
  });

  it("enUnidadDeVenta vuelve al texto del campo", () => {
    expect(enUnidadDeVenta(750, 1000)).toBe("0,75");
    expect(enUnidadDeVenta(2000, 1000)).toBe("2");
    expect(enUnidadDeVenta(7, 1)).toBe("7");
  });
});
