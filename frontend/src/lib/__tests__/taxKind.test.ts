import { TAX_KINDS, TAX_KIND_LABELS, taxKindParaEnviar } from "@/lib/expenseCategories";

describe("taxKindParaEnviar (M·3-3)", () => {
  it("manda el tipo sólo con categoría Impuestos", () => {
    expect(taxKindParaEnviar("TAXES", "iibb")).toBe("iibb");
    expect(taxKindParaEnviar("RENT", "iibb")).toBeNull();
  });

  it("sin elegir manda null (sin tipo)", () => {
    expect(taxKindParaEnviar("TAXES", "")).toBeNull();
    expect(taxKindParaEnviar("TAXES", null)).toBeNull();
    expect(taxKindParaEnviar("TAXES", undefined)).toBeNull();
  });

  it("espeja el catálogo del backend", () => {
    expect(TAX_KINDS).toEqual(["iibb", "monotributo", "iva", "tasa_municipal", "otro"]);
    expect(TAX_KIND_LABELS.iva).toBe("IVA");
  });
});
