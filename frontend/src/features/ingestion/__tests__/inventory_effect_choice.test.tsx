import "@testing-library/jest-dom";
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";

import { InventoryEffectChoice } from "../InventoryEffectChoice";
import type { SheetInventoryEffect } from "@/services/ingestion.service";

/**
 * F-H3.e — el control que hacía falta para que `historical_replay` exista fuera
 * de la API.
 *
 * Lo que estos tests protegen no es el HTML: es que las opciones y sus textos
 * salgan de lo que sirve el backend. Una lista propia acá ofrecería modos que el
 * importador no honra, que es exactamente cómo se rompió el mapeo de columnas.
 */

const VENTAS: SheetInventoryEffect = {
  context_id: "sheet:Ventas",
  label: "Ventas marzo",
  default: "informational",
  options: [
    { value: "informational", label: "Estas filas no modificarán el inventario" },
    { value: "historical_replay", label: "Aplicar la historia: las compras suman y las ventas restan" },
    { value: "no_inventory", label: "Estas cantidades no afectan el inventario" },
  ],
};

const SIN_DECISION: SheetInventoryEffect = {
  context_id: "sheet:Clientes",
  label: "Clientes",
  default: "no_inventory",
  options: [{ value: "no_inventory", label: "Estas cantidades no afectan el inventario" }],
};

describe("InventoryEffectChoice", () => {
  it("ofrece exactamente las opciones que mandó el backend, con sus textos", () => {
    render(<InventoryEffectChoice hoja={VENTAS} value="informational" onChange={jest.fn()} />);

    for (const opt of VENTAS.options) {
      expect(screen.getByRole("button", { name: opt.label })).toBeInTheDocument();
    }
    expect(screen.getAllByRole("button")).toHaveLength(VENTAS.options.length);
  });

  it("marca la opción activa y avisa el cambio", () => {
    const onChange = jest.fn();
    render(<InventoryEffectChoice hoja={VENTAS} value="informational" onChange={onChange} />);

    const informativo = screen.getByRole("button", { name: VENTAS.options[0]!.label });
    expect(informativo).toHaveAttribute("aria-pressed", "true");

    fireEvent.click(screen.getByRole("button", { name: VENTAS.options[1]!.label }));
    expect(onChange).toHaveBeenCalledWith("historical_replay");
  });

  it("avisa que aplicar la historia es lo único que toca el stock", () => {
    // El resto de los modos calculan y muestran. Este escribe: si el usuario no
    // se entera, descubre el cambio de inventario después de que pasó.
    render(
      <InventoryEffectChoice hoja={VENTAS} value="historical_replay" onChange={jest.fn()} />,
    );

    expect(screen.getByText(/único modo que modifica el stock/i)).toBeInTheDocument();
    expect(screen.getByText(/quedan en «Otros»/i)).toBeInTheDocument();
  });

  it("sin aplicar la historia no muestra el aviso", () => {
    render(<InventoryEffectChoice hoja={VENTAS} value="informational" onChange={jest.fn()} />);
    expect(screen.queryByText(/único modo que modifica el stock/i)).not.toBeInTheDocument();
  });

  it("con una sola opción informa y no ofrece un control que no decide nada", () => {
    render(
      <InventoryEffectChoice hoja={SIN_DECISION} value="no_inventory" onChange={jest.fn()} />,
    );

    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.getByText(/no afectan el inventario/i)).toBeInTheDocument();
  });

  it("el copy habla de ESTA hoja, nunca del archivo", () => {
    // Un catálogo puede dejar el stock en su saldo mientras las ventas de la
    // hoja de al lado no lo descuentan: decir "este archivo" sería falso.
    const { container } = render(
      <InventoryEffectChoice hoja={VENTAS} value="informational" onChange={jest.fn()} />,
    );
    expect(container.textContent).toContain("esta hoja");
    expect(container.textContent).not.toContain("este archivo");
  });
});
