import { humanizeColumnLabel } from "../columnLabels";

describe("humanizeColumnLabel", () => {
  it("convierte una columna sin encabezado (col_N) a texto legible", () => {
    expect(humanizeColumnLabel("col_8")).toBe("Columna sin encabezado 8");
    expect(humanizeColumnLabel("col_0")).toBe("Columna sin encabezado 0");
  });

  it("deja intacto un nombre de columna real, incluso si contiene 'col'", () => {
    expect(humanizeColumnLabel("fecha")).toBe("fecha");
    expect(humanizeColumnLabel("Precio de compra")).toBe("Precio de compra");
    expect(humanizeColumnLabel("colegio")).toBe("colegio");
    expect(humanizeColumnLabel("col_8x")).toBe("col_8x");
  });
});
