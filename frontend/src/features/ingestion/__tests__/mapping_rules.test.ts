/**
 * Las reglas que evitan que la UI muestre un estado distinto del que envía.
 *
 * Cada caso reproduce una forma concreta del incidente ASTERIA (2026-07-31),
 * donde el panel daba el OK y el confirm devolvía 422 tres veces seguidas.
 */

import {
  coversRequired,
  missingRequiredFields,
  scalarCollisions,
} from "../mappingRules";
import type { FieldCatalogEntry } from "@/services/ingestion.service";

const CAMPOS_PRODUCTO: FieldCatalogEntry[] = [
  { value: "name", label: "Nombre", single_value: false },
  { value: "sale_price_ars", label: "Precio de venta", single_value: true },
  { value: "list_price_ars", label: "Precio de lista (sugerido)", single_value: true },
  { value: "unit_cost_ars", label: "Costo unitario", single_value: true },
  { value: "stock_units", label: "Stock (unidades)", single_value: true },
  { value: "description", label: "Descripción", single_value: false },
];

describe("coversRequired", () => {
  it("un campo canónico cubre el requerido", () => {
    expect(coversRequired("name")).toBe(true);
  });

  it("un campo personalizado NO cubre el requerido", () => {
    // El caso exacto: el usuario movió la columna «Productos» a un campo propio
    // y la UI lo daba por mapeado mientras el backend lo contaba como faltante.
    expect(coversRequired("custom_field:nombre_del_producto")).toBe(false);
  });

  it("«ignorar» y vacío no cubren nada", () => {
    expect(coversRequired("ignore")).toBe(false);
    expect(coversRequired("")).toBe(false);
  });
});

describe("missingRequiredFields", () => {
  it("detecta el requerido que quedó en un campo personalizado", () => {
    const faltan = missingRequiredFields(["name"], {
      Productos: "custom_field:nombre_del_producto",
      "Precio de venta final": "sale_price_ars",
    });
    expect(faltan).toEqual(["name"]);
  });

  it("no reporta nada cuando el requerido está en su campo canónico", () => {
    expect(
      missingRequiredFields(["name"], {
        Productos: "name",
        "Precio de compra": "unit_cost_ars",
      }),
    ).toEqual([]);
  });

  it("alcanza con que UNA columna cubra el requerido", () => {
    expect(
      missingRequiredFields(["name"], {
        Alias: "custom_field:alias",
        Productos: "name",
      }),
    ).toEqual([]);
  });

  it("reporta varios requeridos faltantes a la vez", () => {
    expect(
      missingRequiredFields(["amount", "transaction_date"], { detalle: "notes" }),
    ).toEqual(["amount", "transaction_date"]);
  });
});

describe("scalarCollisions", () => {
  it("detecta las tres columnas de precio apuntando al mismo campo", () => {
    // El mapeo que traía el archivo real: las tres a `sale_price_ars`.
    const colisiones = scalarCollisions(CAMPOS_PRODUCTO, {
      Productos: "name",
      "Precio de compra": "sale_price_ars",
      "Precio de lista": "sale_price_ars",
      "Precio de venta final": "sale_price_ars",
    });

    expect(colisiones).toHaveLength(1);
    expect(colisiones[0]?.label).toBe("Precio de venta");
    expect(colisiones[0]?.columns).toEqual([
      "Precio de compra",
      "Precio de lista",
      "Precio de venta final",
    ]);
  });

  it("no hay colisión con cada precio en su campo", () => {
    expect(
      scalarCollisions(CAMPOS_PRODUCTO, {
        Productos: "name",
        "Precio de compra": "unit_cost_ars",
        "Precio de lista": "list_price_ars",
        "Precio de venta final": "sale_price_ars",
      }),
    ).toEqual([]);
  });

  it("un campo no escalar admite varias columnas", () => {
    // Bloquear todo sería tan malo como no bloquear nada: trabaría imports
    // legítimos donde dos columnas alimentan un mismo texto.
    expect(
      scalarCollisions(CAMPOS_PRODUCTO, {
        Especificaciones: "description",
        Detalle: "description",
      }),
    ).toEqual([]);
  });

  it("qué campo es escalar lo decide el catálogo, no una lista propia", () => {
    const sinEscalares = CAMPOS_PRODUCTO.map((f) => ({ ...f, single_value: false }));
    expect(
      scalarCollisions(sinEscalares, {
        a: "sale_price_ars",
        b: "sale_price_ars",
      }),
    ).toEqual([]);
  });

  it("reporta varias colisiones independientes", () => {
    const colisiones = scalarCollisions(CAMPOS_PRODUCTO, {
      a: "sale_price_ars",
      b: "sale_price_ars",
      c: "stock_units",
      d: "stock_units",
    });
    expect(colisiones.map((c) => c.target).sort()).toEqual([
      "sale_price_ars",
      "stock_units",
    ]);
  });
});
