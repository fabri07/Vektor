import type { UnclassifiedRecordResponse } from "@/services/others.service";
import { agruparPorReferencia, esQueryAfectadaPorVincular, prefill, toIsoDate } from "../helpers";

describe("toIsoDate", () => {
  it("normaliza dd/mm/yyyy inequívoco a ISO", () => {
    // 13/03/2024: 13 no puede ser mes.
    expect(toIsoDate("13/03/2024")).toBe("2024-03-13");
    // Mismo número de los dos lados: las dos lecturas son el mismo día.
    expect(toIsoDate("04/04/2026")).toBe("2026-04-04");
  });

  it("V-16: una fecha corta ambigua no se adivina (queda vacía)", () => {
    // 3 de abril o 4 de marzo: la escribe la persona.
    expect(toIsoDate("03/04/2026")).toBe("");
    expect(toIsoDate("12/03/2024")).toBe("");
    expect(toIsoDate("05-06-26")).toBe("");
  });

  it("acepta guiones y año de 2 dígitos con el pivote de strptime (69)", () => {
    expect(toIsoDate("15-06-26")).toBe("2026-06-15");
    expect(toIsoDate("15/06/26")).toBe("2026-06-15");
    // Pivote 69: 00–68 → 20xx, 69–99 → 19xx (igual que el backend).
    expect(toIsoDate("15/06/68")).toBe("2068-06-15");
    expect(toIsoDate("15/06/69")).toBe("1969-06-15");
    expect(toIsoDate("15/06/99")).toBe("1999-06-15");
  });

  it("rechaza fechas calendáricamente imposibles", () => {
    expect(toIsoDate("31/02/2024")).toBe(""); // 31 de febrero no existe
    expect(toIsoDate("30/02/2024")).toBe("");
  });

  it("deja pasar ISO (con o sin hora)", () => {
    expect(toIsoDate("2024-01-15")).toBe("2024-01-15");
    expect(toIsoDate("2024-01-15T14:30:00")).toBe("2024-01-15");
  });

  it("cae a mm/dd solo cuando el 2º campo no puede ser mes", () => {
    expect(toIsoDate("04/13/2026")).toBe("2026-04-13");
  });

  it("devuelve '' para vacío o formato irreconocible", () => {
    expect(toIsoDate("")).toBe("");
    expect(toIsoDate("no es fecha")).toBe("");
    expect(toIsoDate("99/99/9999")).toBe("");
  });
});

describe("prefill", () => {
  it("prellena la fecha en ISO para que el <input type=date> no quede vacío", () => {
    const record = {
      id: "r1",
      uploaded_file_id: null,
      source: "ingestion",
      context_label: "Fila sin fecha reconocible",
      headers: ["fecha", "monto"],
      row_data: { fecha: "13/03/2024", monto: "5000" },
      suggested_entity: "sale",
      suggested_category: null,
      suggested_category_label: null,
      match_candidates: null,
      status: "PENDING",
      created_at: "2026-07-21T10:00:00Z",
    } as unknown as UnclassifiedRecordResponse;
    const pre = prefill(record);
    expect(pre.date).toBe("2024-03-13");
    expect(pre.amount).toBe("5000");
  });

  it("V-16: con una fecha ambigua deja la fecha vacía (no la adivina)", () => {
    const record = {
      id: "r2",
      row_data: { fecha: "04/03/2026", monto: "5000" },
    } as unknown as UnclassifiedRecordResponse;
    expect(prefill(record).date).toBe("");
  });
});

describe("agruparPorReferencia (B·8b)", () => {
  const venta = (id: string, producto: string | undefined) =>
    ({
      id,
      row_data: producto === undefined ? { monto: "10" } : { producto, monto: "10" },
      suggested_entity: "sale",
    }) as unknown as UnclassifiedRecordResponse;

  it("agrupa por texto sin mayúsculas ni espacios, muestra el crudo y ordena por cantidad", () => {
    const grupos = agruparPorReferencia([
      venta("a", "Yerba"),
      venta("b", "Coca 500"),
      venta("c", " coca  500 "),
      venta("d", ""),
      venta("e", undefined),
    ]);
    expect(grupos.map((g) => [g.referencia, g.records.map((r) => r.id)])).toEqual([
      ["Coca 500", ["b", "c"]],
      ["Yerba", ["a"]],
    ]);
  });
});

describe("esQueryAfectadaPorVincular (B·8b)", () => {
  it("invalida «Otros», productos y ventas, no las categorías ni el resto", () => {
    for (const k of ["others-pending", "others-link-queue", "products", "products-list", "sales-entries", "sales-all"]) {
      expect(esQueryAfectadaPorVincular([k])).toBe(true);
    }
    for (const k of ["product-categories", "health-score", "expenses", "salesman"]) {
      expect(esQueryAfectadaPorVincular([k])).toBe(false);
    }
  });
});
