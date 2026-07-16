import "@testing-library/jest-dom";
import React from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { PublicNav } from "../PublicNav";

function rubrosTrigger() {
  return screen.getByRole("button", { name: /Rubros/i });
}

describe("PublicNav — dropdown Rubros (disclosure accesible)", () => {
  test("click abre y muestra las verticales; no usa role=menu", async () => {
    const user = userEvent.setup();
    render(<PublicNav />);
    const trigger = rubrosTrigger();
    expect(trigger).toHaveAttribute("aria-expanded", "false");

    await user.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("link", { name: /Kiosco \/ Almacén/i })).toBeInTheDocument();
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
    const firstItem = screen.getByRole("link", { name: /Kiosco \/ Almacén/i });
    expect(firstItem).toHaveFocus();
  });

  test("las flechas mueven el foco entre items", async () => {
    const user = userEvent.setup();
    render(<PublicNav />);
    rubrosTrigger().focus();
    await user.keyboard("{ArrowDown}"); // abre + primer item
    await user.keyboard("{ArrowDown}"); // segundo item
    expect(screen.getByRole("link", { name: /^Limpieza$/i })).toHaveFocus();
    await user.keyboard("{ArrowUp}"); // vuelve al primero
    expect(screen.getByRole("link", { name: /Kiosco \/ Almacén/i })).toHaveFocus();
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
