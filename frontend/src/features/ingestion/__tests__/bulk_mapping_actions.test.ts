import "@testing-library/jest-dom";
import {
  acceptAmbiguousSuggestions,
  customFieldSlug,
  ignoreEmptyColumns,
  saveUnmappedAsCustomFields,
} from "../bulkMappingActions";
import type { ColumnMappingSuggestion } from "@/services/ingestion.service";

function s(over: Partial<ColumnMappingSuggestion>): ColumnMappingSuggestion {
  return {
    source_column: "Col",
    normalized_column: "col",
    sample_values: ["x"],
    target_field: null,
    confidence: 0.5,
    source: "none",
    status: "unmapped",
    ...over,
  };
}

describe("customFieldSlug", () => {
  it("normaliza espacios y mayúsculas", () => {
    expect(customFieldSlug("Año Fiscal")).toBe("custom_field:año_fiscal");
    expect(customFieldSlug("  Obs libres  ")).toBe("custom_field:obs_libres");
  });
});

describe("acceptAmbiguousSuggestions", () => {
  it("toma el candidato de mayor confianza de cada columna ambigua sin resolver", () => {
    const suggestions = [
      s({ source_column: "Fecha", status: "ambiguo", options: ["transaction_date", "created_at"] }),
      s({ source_column: "Precio", status: "ambiguo", options: ["unit_price", "amount"] }),
    ];
    const updates = acceptAmbiguousSuggestions(suggestions, {});
    expect(updates).toEqual({ Fecha: "transaction_date", Precio: "unit_price" });
  });

  it("no toca una columna ambigua que el usuario ya resolvió", () => {
    const suggestions = [
      s({ source_column: "Fecha", status: "ambiguo", options: ["transaction_date", "created_at"] }),
    ];
    const updates = acceptAmbiguousSuggestions(suggestions, { Fecha: "created_at" });
    expect(updates).toEqual({});
  });

  it("ignora columnas unmapped (sin candidatos) y mapped (ya resueltas)", () => {
    const suggestions = [
      s({ source_column: "SinNada", status: "unmapped" }),
      s({ source_column: "YaMapeada", status: "mapped", target_field: "amount" }),
    ];
    const updates = acceptAmbiguousSuggestions(suggestions, { YaMapeada: "amount" });
    expect(updates).toEqual({});
  });

  it("una ambigua sin options no propone nada (no hay candidato de qué agarrarse)", () => {
    const suggestions = [s({ source_column: "Rara", status: "ambiguo", options: [] })];
    expect(acceptAmbiguousSuggestions(suggestions, {})).toEqual({});
  });
});

describe("saveUnmappedAsCustomFields", () => {
  it("convierte cada columna sin target en campo propio con su nombre", () => {
    const suggestions = [
      s({ source_column: "Observaciones internas", status: "unmapped" }),
      s({ source_column: "Otra Col", status: "unmapped" }),
    ];
    const updates = saveUnmappedAsCustomFields(suggestions, {});
    expect(updates).toEqual({
      "Observaciones internas": "custom_field:observaciones_internas",
      "Otra Col": "custom_field:otra_col",
    });
  });

  it("no toca lo ya mapeado ni lo ambiguo (tiene candidatos concretos, no es 'no reconocida')", () => {
    const suggestions = [
      s({ source_column: "Mapeada", status: "mapped", target_field: "amount" }),
      s({ source_column: "Ambigua", status: "ambiguo", options: ["a", "b"] }),
    ];
    const updates = saveUnmappedAsCustomFields(suggestions, { Mapeada: "amount" });
    expect(updates).toEqual({});
  });

  it("una columna sin ninguna muestra real queda afuera — es candidata de ignorar, no de esta", () => {
    const suggestions = [
      s({ source_column: "Vacia", status: "unmapped", sample_values: [] }),
      s({ source_column: "ConDatos", status: "unmapped", sample_values: ["algo"] }),
    ];
    const updates = saveUnmappedAsCustomFields(suggestions, {});
    expect(updates).toEqual({ ConDatos: "custom_field:condatos" });
  });

  it("no pisa una columna que el usuario ya marcó ignore o custom a mano", () => {
    const suggestions = [s({ source_column: "Col", status: "unmapped" })];
    expect(saveUnmappedAsCustomFields(suggestions, { Col: "ignore" })).toEqual({});
    expect(
      saveUnmappedAsCustomFields(suggestions, { Col: "custom_field:mi_propio_nombre" }),
    ).toEqual({});
  });
});

describe("ignoreEmptyColumns", () => {
  it("ignora columnas sin target y sin ninguna muestra con contenido real", () => {
    const suggestions = [
      s({ source_column: "Vacia1", status: "unmapped", sample_values: [] }),
      s({ source_column: "Vacia2", status: "unmapped", sample_values: ["", "  ", "nan", "NaN"] }),
      s({ source_column: "ConDatos", status: "unmapped", sample_values: ["Agua mineral"] }),
    ];
    const updates = ignoreEmptyColumns(suggestions, {});
    expect(updates).toEqual({ Vacia1: "ignore", Vacia2: "ignore" });
  });

  it("no toca una columna vacía que el usuario ya mapeó a mano", () => {
    const suggestions = [s({ source_column: "Col", status: "unmapped", sample_values: [] })];
    expect(ignoreEmptyColumns(suggestions, { Col: "notes" })).toEqual({});
  });

  it("una sola muestra con contenido real alcanza para NO ignorarla", () => {
    const suggestions = [
      s({ source_column: "Col", status: "unmapped", sample_values: ["", "Agua", ""] }),
    ];
    expect(ignoreEmptyColumns(suggestions, {})).toEqual({});
  });
});

describe("las tres acciones combinadas no pisan cambios de la anterior", () => {
  it("primero ignorar-vacías deja esa columna afuera de guardar-como-propio", () => {
    const suggestions = [s({ source_column: "Vacia", status: "unmapped", sample_values: [] })];
    const afterIgnore = ignoreEmptyColumns(suggestions, {});
    const mappingsTrasIgnorar = { ...afterIgnore };
    const afterCustom = saveUnmappedAsCustomFields(suggestions, mappingsTrasIgnorar);
    expect(afterCustom).toEqual({});
  });
});
