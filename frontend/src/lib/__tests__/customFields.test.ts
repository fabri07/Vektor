import { formatCustomFieldValue } from "@/lib/customFields";

/**
 * Cambio 2 del plan de conservación: un valor presente que no encaja en el
 * tipo declarado se muestra ORIGINAL, nunca como un guión — un guión dice
 * "no hay dato", y acá sí lo hay.
 */

describe("formatCustomFieldValue", () => {
  test("null y vacío son los ÚNICOS casos que muestran guión", () => {
    expect(formatCustomFieldValue(null, "text", null)).toBe("—");
    expect(formatCustomFieldValue("", "number", null)).toBe("—");
    expect(formatCustomFieldValue(undefined, "boolean", null)).toBe("—");
  });

  test("0 y false NO son guión — son valores válidos y distintos de vacío", () => {
    expect(formatCustomFieldValue(0, "number", null)).toBe("0");
    expect(formatCustomFieldValue(false, "boolean", null)).toBe("No");
  });

  test("número: un texto que no parsea muestra el original, no un guión", () => {
    expect(formatCustomFieldValue("doce mil", "number", null)).toBe("doce mil");
  });

  test("boolean: un valor que no es Sí/No conocido muestra el original", () => {
    expect(formatCustomFieldValue("tal vez", "boolean", null)).toBe("tal vez");
    expect(formatCustomFieldValue(2, "boolean", null)).toBe("2");
  });

  test("fecha inválida muestra el original (ya lo hacía, se deja fijado)", () => {
    expect(formatCustomFieldValue("no es una fecha", "date", null)).toBe("no es una fecha");
  });

  test("enum sin coincidencia muestra el valor crudo, no una etiqueta inventada", () => {
    expect(
      formatCustomFieldValue("valor_viejo", "enum", [{ value: "nuevo", label: "Nuevo" }]),
    ).toBe("valor_viejo");
  });

  test("número válido se formatea en es-AR", () => {
    expect(formatCustomFieldValue(1234.5, "number", null)).toBe("1.234,5");
  });

  test("un objeto/lista se muestra como JSON legible, no [object Object]", () => {
    expect(formatCustomFieldValue({ a: 1 }, "text", null)).toBe('{"a":1}');
    expect(formatCustomFieldValue([1, 2], "text", null)).toBe("[1,2]");
  });
});
