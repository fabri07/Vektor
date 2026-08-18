import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";

import { SmartTable } from "../SmartTable";
import { Table } from "../Table";

/**
 * F-V.1 — la tabla se podía scrollear pero no había cómo.
 *
 * Medido en Chrome antes de tocar nada (viewport 1440×900, 50 filas, las 9
 * columnas de /sales): el contenedor SÍ scrolleaba (`scrollWidth` 1328 vs
 * `clientWidth` 1200) y la barra existía, ocupando sus 15px de layout. El
 * problema era dónde: al fondo de un contenedor de 2055px de alto, o sea 1375px
 * por debajo del pliegue. Y sin `tabindex`, las flechas del teclado tampoco
 * llegaban. Desde afuera eso se ve igual que "no se puede scrollear".
 *
 * jsdom no hace layout, así que lo que se puede verificar acá son los hechos del
 * DOM que habilitan la solución (foco, semántica, columna fija). La geometría
 * —que la columna de acciones queda clavada al scrollear— se midió en un
 * navegador real, no en este archivo.
 */

const columnas = [
  { key: "fecha", header: "Fecha" },
  { key: "monto", header: "Monto" },
];
const filas = [{ fecha: "08/08/2026", monto: "4000" }];

describe("F-V.1 · el área desplazable es alcanzable", () => {
  test("el contenedor se puede enfocar con el teclado y se anuncia", () => {
    render(<Table columns={columnas} data={filas} ariaLabel="Ventas" />);

    const region = screen.getByRole("region", { name: "Ventas" });
    // Sin tabIndex, las flechas no mueven un div desplazable: era la queja
    // literal del usuario ("no puedo desplazarme apretando la flecha").
    expect(region).toHaveAttribute("tabindex", "0");
    expect(region.className).toContain("overflow-x-auto");
  });

  test("los gradientes no se dibujan cuando no hay nada más para ver", () => {
    // Estaban SIEMPRE encendidos: no indicaban desborde, decoraban — y encima
    // tapaban la última columna simulando un borde. En jsdom no hay desborde
    // posible (scrollWidth === clientWidth === 0), así que no debe haber ninguno.
    const { container } = render(<Table columns={columnas} data={filas} />);

    expect(container.querySelectorAll('[aria-hidden="true"]')).toHaveLength(0);
  });

  test("la columna de acciones queda fija a la derecha", () => {
    render(
      <SmartTable
        columns={columnas}
        data={filas}
        renderActions={() => <button type="button">Editar</button>}
      />,
    );

    const celda = screen.getByRole("button", { name: "Editar" }).closest("td");
    // Era la columna que el scroll cortaba, y es la que tiene los botones.
    expect(celda?.className).toContain("sticky");
    expect(celda?.className).toContain("right-0");
  });

  test("sin acciones, ninguna columna se fija", () => {
    const { container } = render(<Table columns={columnas} data={filas} />);

    expect(container.querySelectorAll("td.sticky")).toHaveLength(0);
  });
});
