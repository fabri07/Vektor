/**
 * Precio y costo son por unidad de VENTA (kg, L) y el stock está en unidades
 * BASE (g, ml): toda cuenta stock × costo divide por el factor. Mismos casos
 * que `backend/app/tests/domain/test_sale_unit.py`.
 */
import { formatearCantidad, valorDeStock } from "@/lib/pos/saleUnit";

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
