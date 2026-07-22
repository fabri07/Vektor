import type { UnclassifiedRecordResponse } from "@/services/others.service";
import { prefill, toIsoDate } from "../helpers";

describe("toIsoDate", () => {
  it("normaliza dd/mm/yyyy a ISO (convención AR)", () => {
    // 12/03/2024 = 12 de marzo, no 3 de diciembre.
    expect(toIsoDate("12/03/2024")).toBe("2024-03-12");
    // 03/04/2026 = 3 de abril (dd/mm antes que mm/dd).
    expect(toIsoDate("03/04/2026")).toBe("2026-04-03");
  });

  it("acepta guiones y año de 2 dígitos con el pivote de strptime (69)", () => {
    expect(toIsoDate("05-06-26")).toBe("2026-06-05");
    expect(toIsoDate("05/06/26")).toBe("2026-06-05");
    // Pivote 69: 00–68 → 20xx, 69–99 → 19xx (igual que el backend).
    expect(toIsoDate("05/06/68")).toBe("2068-06-05");
    expect(toIsoDate("05/06/69")).toBe("1969-06-05");
    expect(toIsoDate("05/06/99")).toBe("1999-06-05");
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
      row_data: { fecha: "12/03/2024", monto: "5000" },
      suggested_entity: "sale",
      suggested_category: null,
      suggested_category_label: null,
      match_candidates: null,
      status: "PENDING",
      created_at: "2026-07-21T10:00:00Z",
    } as unknown as UnclassifiedRecordResponse;
    const pre = prefill(record);
    expect(pre.date).toBe("2024-03-12");
    expect(pre.amount).toBe("5000");
  });
});
