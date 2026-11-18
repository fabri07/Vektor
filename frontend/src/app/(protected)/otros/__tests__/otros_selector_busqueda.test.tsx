/**
 * La búsqueda del selector de producto no distingue plural, tildes ni mayúsculas:
 * «Buzo canguro» (lo que dijo el archivo) tiene que encontrar «buzos canguro». Caso real:
 * el único producto con respaldo a la fecha de la venta quedaba fuera de la lista.
 */
import React from "react";
import { render, screen } from "@testing-library/react";

import { ComboVentaLineas } from "../ComboVentaLineas";
import { ProductoVendidoSelector, plegarBusqueda } from "../ProductoVendidoSelector";

const PRODUCTOS = [
  { id: "p-gris", name: "Buzo canguro gris", primer_dia_de_fuente: "2026-11-05" },
  { id: "p-buzos", name: "buzos canguro", primer_dia_de_fuente: "2026-07-18" },
  { id: "p-jean", name: "Campera rompeviento", primer_dia_de_fuente: "2026-09-01" },
];

function opciones(busqueda: string, dia?: string): string[] {
  render(
    <ProductoVendidoSelector
      productos={PRODUCTOS}
      busqueda={busqueda}
      onBusqueda={jest.fn()}
      value=""
      onChange={jest.fn()}
      referencia=""
      inputClassName=""
      diaDeLaVenta={dia}
    />,
  );
  return Array.from(
    (screen.getByLabelText("Producto vendido") as HTMLSelectElement).options,
  ).map((o) => o.text);
}

test("plegarBusqueda iguala plural, tildes y mayúsculas", () => {
  expect(plegarBusqueda("Buzos  Canguro")).toBe(plegarBusqueda("buzo canguro"));
  expect(plegarBusqueda("Camperás")).toBe(plegarBusqueda("campera"));
  expect(plegarBusqueda("Jeans")).toBe("jean");
});

test("«Buzo canguro» encuentra también «buzos canguro»", () => {
  const o = opciones("Buzo canguro");
  expect(o.some((t) => t.startsWith("buzos canguro"))).toBe(true);
  expect(o.some((t) => t.startsWith("Buzo canguro gris"))).toBe(true);
  expect(o.some((t) => t.startsWith("Campera"))).toBe(false);
});

test("con el día de la venta, el producto con respaldo va primero", () => {
  const o = opciones("buzo canguro", "2026-08-25");
  expect(o[1]).toBe("buzos canguro");
  expect(o[2]).toMatch(/Buzo canguro gris · sin compra ni stock antes del 05\/11\/2026/);
});

describe("selector de cada línea del combo", () => {
  test("marca los productos sin respaldo a la fecha y pone primero los respaldados", () => {
    const productos = [
      { id: "p-nov", name: "jean azul T40", primer_dia_de_fuente: "2026-11-05" },
      { id: "p-ago", name: "Jeans azules T40", primer_dia_de_fuente: "2026-08-02" },
    ];
    render(
      <ComboVentaLineas
        productos={productos}
        lineas={[
          { productId: "", quantity: "1", unitPrice: "" },
          { productId: "", quantity: "1", unitPrice: "" },
        ]}
        onLineas={jest.fn()}
        montoDeLaFila={23000}
        sugerencias={undefined}
        mostrarErrores={false}
        onVolver={jest.fn()}
        inputClassName=""
        diaDeLaVenta="2026-08-03"
      />,
    );
    const o = Array.from(
      (screen.getByLabelText("Producto de la línea 1") as HTMLSelectElement).options,
    ).map((x) => x.text);
    expect(o[1]).toBe("Jeans azules T40");
    expect(o[2]).toBe("jean azul T40 · sin compra ni stock antes del 05/11/2026");
  });
});
