import "@testing-library/jest-dom";
import React from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { VERTICAL_OPTIONS } from "@/lib/verticals";

import { PublicNav } from "../PublicNav";

function rubrosTrigger() {
  return screen.getByRole("button", { name: /Soluciones por rubro/i });
}

/**
 * Los items salen de `VERTICAL_OPTIONS`, la misma fuente que arma el dropdown
 * (`PublicNav.tsx`), en vez de hardcodear el nombre del rubro: acá el texto sólo
 * identifica "el primer item" / "el segundo" para verificar el foco, no es lo
 * que el test viene a probar. Mismo patrón que `rubros_page.test.tsx`.
 */
function nombreDeRubro(indice: number): string {
  const rubro = VERTICAL_OPTIONS[indice];
  if (!rubro) {
    throw new Error(`No hay un rubro en la posición ${indice} de VERTICAL_OPTIONS.`);
  }
  return rubro.name;
}

const PRIMER_RUBRO = nombreDeRubro(0);
const SEGUNDO_RUBRO = nombreDeRubro(1);

describe("PublicNav — dropdown Rubros (disclosure accesible)", () => {
  test("click abre y muestra las verticales; no usa role=menu", async () => {
    const user = userEvent.setup();
    render(<PublicNav />);
    const trigger = rubrosTrigger();
    expect(trigger).toHaveAttribute("aria-expanded", "false");

    await user.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("link", { name: PRIMER_RUBRO })).toBeInTheDocument();
    // No prometemos semántica de menú.
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem")).not.toBeInTheDocument();
  });

  test("ArrowDown desde el botón abre y enfoca el primer item", async () => {
    const user = userEvent.setup();
    render(<PublicNav />);
    const trigger = rubrosTrigger();
    trigger.focus();
    await user.keyboard("{ArrowDown}");

    expect(trigger).toHaveAttribute("aria-expanded", "true");
    const firstItem = screen.getByRole("link", { name: PRIMER_RUBRO });
    expect(firstItem).toHaveFocus();
  });

  test("las flechas mueven el foco entre items", async () => {
    const user = userEvent.setup();
    render(<PublicNav />);
    rubrosTrigger().focus();
    await user.keyboard("{ArrowDown}"); // abre + primer item
    await user.keyboard("{ArrowDown}"); // segundo item
    expect(screen.getByRole("link", { name: SEGUNDO_RUBRO })).toHaveFocus();
    await user.keyboard("{ArrowUp}"); // vuelve al primero
    expect(screen.getByRole("link", { name: PRIMER_RUBRO })).toHaveFocus();
  });

  test("Escape cierra el dropdown y devuelve el foco al botón", async () => {
    const user = userEvent.setup();
    render(<PublicNav />);
    const trigger = rubrosTrigger();
    trigger.focus();
    await user.keyboard("{ArrowDown}");
    expect(trigger).toHaveAttribute("aria-expanded", "true");

    await user.keyboard("{Escape}");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(trigger).toHaveFocus();
  });
});
