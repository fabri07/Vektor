import {
  buildAllDataRows,
  buildAvailableFieldColumns,
  readByValuePath,
} from "@/lib/fieldCatalog";
import type { AvailableField } from "@/services/fieldCatalog.service";

/**
 * Caso real que motivó la revisión: "Color"/"Estilo" (campos del rubro sin
 * columna real) y "Marca" (recuperada por el backfill) tienen que verse en
 * la tabla y en el detalle, leyendo el valor correcto desde `custom_fields`.
 */

function campo(overrides: Partial<AvailableField>): AvailableField {
  return {
    field_id: "product:x",
    entity_type: "product",
    field_key: "x",
    label: "X",
    data_type: "text",
    unit: null,
    origin: "additional",
    value_path: "x",
    enum_options: null,
    editable: true,
    exportable: true,
    searchable: true,
    default_visible: true,
    enabled_for_new_entries: true,
    financial_rule: null,
    ...overrides,
  };
}

describe("readByValuePath", () => {
  test("lee un atributo real de la fila", () => {
    expect(readByValuePath({ unit_cost_ars: 9000 }, "unit_cost_ars")).toBe(9000);
  });

  test("lee una clave de custom_fields", () => {
    expect(readByValuePath({ custom_fields: { color: "Rojo" } }, "custom_fields.color")).toBe(
      "Rojo",
    );
  });

  test("custom_fields ausente no explota, devuelve undefined", () => {
    expect(readByValuePath({}, "custom_fields.color")).toBeUndefined();
  });

  test("fila null/undefined no explota", () => {
    expect(readByValuePath(null, "custom_fields.color")).toBeUndefined();
    expect(readByValuePath(undefined, "name")).toBeUndefined();
  });
});

describe("buildAvailableFieldColumns", () => {
  const COLOR = campo({
    field_id: "product:custom_fields.color",
    field_key: "color",
    label: "Color",
    origin: "canonical", // campo del rubro, aunque viva en custom_fields
    value_path: "custom_fields.color",
  });
  const ESTILO = campo({
    field_id: "product:custom_fields.style",
    field_key: "style",
    label: "Estilo",
    origin: "canonical",
    value_path: "custom_fields.style",
    data_type: "enum",
    enum_options: [{ value: "moderno", label: "Moderno" }],
  });
  const MARCA = campo({
    field_id: "product:custom_fields.marca",
    field_key: "marca",
    label: "Marca",
    origin: "additional",
    value_path: "custom_fields.marca",
  });
  const YA_HARDCODEADO = campo({
    field_id: "product:unit_cost_ars",
    field_key: "unit_cost_ars",
    label: "Costo unitario",
    origin: "canonical",
    value_path: "unit_cost_ars",
  });

  const row = {
    custom_fields: { color: "Rojo", style: "moderno", marca: "El pasillo" },
    unit_cost_ars: 9000,
  };

  test("Color, Estilo y Marca se ven con el valor correcto", () => {
    const cols = buildAvailableFieldColumns(
      [COLOR, ESTILO, MARCA, YA_HARDCODEADO],
      new Set(["product:unit_cost_ars"]), // ya cubierta por una columna a mano
    );
    const porKey = Object.fromEntries(cols.map((c) => [c.key, c]));

    expect(Object.keys(porKey)).not.toContain("product:unit_cost_ars");
    expect(porKey["product:custom_fields.color"]!.render?.(undefined, row)).toBe("Rojo");
    expect(porKey["product:custom_fields.style"]!.render?.(undefined, row)).toBe("Moderno");
    expect(porKey["product:custom_fields.marca"]!.render?.(undefined, row)).toBe("El pasillo");
  });

  test("un adicional arranca oculto aunque el backend diga default_visible", () => {
    const cols = buildAvailableFieldColumns([MARCA], new Set());
    expect(cols[0]!.defaultVisible).toBe(false);
  });

  test("un canónico respeta su propio default_visible", () => {
    const visible = buildAvailableFieldColumns([COLOR], new Set());
    expect(visible[0]!.defaultVisible).toBe(true);

    const oculto = buildAvailableFieldColumns(
      [{ ...COLOR, default_visible: false }],
      new Set(),
    );
    expect(oculto[0]!.defaultVisible).toBe(false);
  });

  test("cero y falso se distinguen de vacío", () => {
    const NUMERICO = campo({
      field_id: "product:custom_fields.n",
      value_path: "custom_fields.n",
      data_type: "number",
    });
    const BOOLEANO = campo({
      field_id: "product:custom_fields.b",
      value_path: "custom_fields.b",
      data_type: "boolean",
    });
    const cols = buildAvailableFieldColumns([NUMERICO, BOOLEANO], new Set());
    const filaConCero = { custom_fields: { n: 0, b: false } };
    const filaVacia = { custom_fields: {} };

    expect(cols[0]!.render?.(undefined, filaConCero)).toBe("0");
    expect(cols[1]!.render?.(undefined, filaConCero)).toBe("No");
    expect(cols[0]!.render?.(undefined, filaVacia)).toBe("—");
    expect(cols[1]!.render?.(undefined, filaVacia)).toBe("—");
  });

  test("un campo no exportable no viaja al CSV aunque se vea en la tabla", () => {
    const cols = buildAvailableFieldColumns([{ ...MARCA, exportable: false }], new Set());
    expect(cols[0]!.csvValue?.(undefined, row)).toBe("");
    expect(cols[0]!.render?.(undefined, row)).toBe("El pasillo");
  });
});

describe("buildAllDataRows", () => {
  test("incluye TODOS los campos, estén o no ocultos en la tabla", () => {
    const disponibles = [
      campo({ field_id: "product:name", value_path: "name", label: "Nombre" }),
      campo({
        field_id: "product:custom_fields.color",
        value_path: "custom_fields.color",
        label: "Color",
        origin: "canonical",
      }),
    ];
    const row = { name: "Silla", custom_fields: { color: "Azul" } };
    const rows = buildAllDataRows(disponibles, row);

    expect(rows).toHaveLength(2);
    expect(rows.find((r) => r.label === "Color")?.formatted).toBe("Azul");
    expect(rows.find((r) => r.label === "Nombre")?.value).toBe("Silla");
  });
});
