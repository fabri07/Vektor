/**
 * Los menús de la cabecera de la caja se cierran con Escape y con un clic
 * afuera: abiertos tapaban el buscador y sólo se cerraban con su rótulo.
 */
import { fireEvent, render, screen } from "@testing-library/react";

import { MenuCabecera } from "@/features/pos/MenuCabecera";

function abrir() {
  render(
    <div>
      <button type="button">Buscar producto</button>
      <MenuCabecera titulo="Impresora">
        <label>
          Papel <select aria-label="Ancho del papel" />
        </label>
      </MenuCabecera>
    </div>,
  );
  const menu = screen.getByText("Impresora").closest("details")!;
  menu.setAttribute("open", "");
  return menu;
}

it("Escape lo cierra", () => {
  const menu = abrir();
  fireEvent.keyDown(document, { key: "Escape" });
  expect(menu.open).toBe(false);
});

it("un clic afuera lo cierra", () => {
  const menu = abrir();
  fireEvent.pointerDown(screen.getByRole("button", { name: "Buscar producto" }));
  expect(menu.open).toBe(false);
});

it("un clic adentro NO lo cierra: se está configurando", () => {
  const menu = abrir();
  fireEvent.pointerDown(screen.getByLabelText("Ancho del papel"));
  expect(menu.open).toBe(true);
});
