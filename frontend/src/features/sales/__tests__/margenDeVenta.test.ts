import { textoMargenDeVenta } from "@/features/sales/margenDeVenta";

// M·3-2: el margen por venta sólo se formatea; ausente nunca es 0.
describe("textoMargenDeVenta", () => {
  it("confirmado y estimado", () => {
    expect(textoMargenDeVenta(50, { margen_estado: "confirmado", margen_motivo: null })).toBe("50,0%");
    expect(textoMargenDeVenta(-3.25, { margen_estado: "estimado", margen_motivo: "x" })).toBe("-3,3% estimado");
  });

  it("sin costo: «—» con el motivo", () => {
    expect(textoMargenDeVenta(null, { margen_estado: "ausente", margen_motivo: "sin_costo" })).toBe(
      "— (sin_costo)",
    );
  });

  it("sin dato del backend (rol sin permiso o cobro de fiado): sólo «—»", () => {
    expect(textoMargenDeVenta(null, { margen_estado: null, margen_motivo: null })).toBe("—");
  });
});
