import "@testing-library/jest-dom";
import React from "react";
import { render, screen } from "@testing-library/react";

import { InventoryEffectChoice } from "../InventoryEffectChoice";
import type { SheetInventoryEffect } from "@/services/ingestion.service";

/**
 * F-F.4 — el control dejó de ser un control.
 *
 * Hasta F-F.3 ofrecía tres modos y el usuario elegía si sus ventas movían el
 * stock. Ahora el efecto se deduce del contenido de la hoja, así que esto
 * informa. Lo que los tests protegen sigue siendo lo mismo: que el texto salga
 * de lo que sirve el backend y no de una tabla propia — una copia acá mostraría
 * lo que el importador no hace, que es como se rompió el mapeo de columnas.
 */

const VENTAS: SheetInventoryEffect = {
  context_id: "sheet:Ventas",
  label: "Ventas marzo",
  default: "historical_replay",
  options: [
    {
      value: "historical_replay",
      label: "Las compras suman y las ventas restan del inventario",
    },
  ],
};

const CATALOGO: SheetInventoryEffect = {
  context_id: "sheet:Catalogo",
  label: "Catálogo",
  default: "current_snapshot",
  options: [
    { value: "current_snapshot", label: "El archivo declara el stock actual (saldo absoluto)" },
  ],
};

/** Gastos fijos, clientes, proveedores, servicios: no hablan de inventario. */
const SIN_INVENTARIO: SheetInventoryEffect = {
  context_id: "sheet:Clientes",
  label: "Clientes",
  default: null,
  options: [],
};

describe("InventoryEffectChoice", () => {
  it("informa el efecto con el texto que mandó el backend", () => {
    render(<InventoryEffectChoice hoja={VENTAS} />);

    expect(screen.getByText(VENTAS.options[0]!.label)).toBeInTheDocument();
  });

  it("no ofrece elegir: el efecto es consecuencia de lo que la hoja contiene", () => {
    render(<InventoryEffectChoice hoja={VENTAS} />);

    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("la hoja que no habla de inventario no renderiza NADA", () => {
    // El pedido textual: sacar «Estas cantidades no afectan el inventario» de
    // Gastos_Fijos, Clientes y Proveedores. La respuesta correcta no es un cartel
    // más suave — es no contestar una pregunta que esa hoja nunca hizo.
    const { container } = render(<InventoryEffectChoice hoja={SIN_INVENTARIO} />);

    expect(container).toBeEmptyDOMElement();
  });

  it("avisa qué pasa con las ventas que no tengan stock que las respalde", () => {
    // Se aplica al confirmar, sin un segundo clic: si el usuario no se entera,
    // descubre después que algunas filas quedaron en «Otros».
    render(<InventoryEffectChoice hoja={VENTAS} />);

    expect(screen.getByText(/se aplica al stock/i)).toBeInTheDocument();
    expect(screen.getByText(/quedan en «Otros»/i)).toBeInTheDocument();
  });

  it("el catálogo no muestra ese aviso: declara un saldo, no descuenta nada", () => {
    render(<InventoryEffectChoice hoja={CATALOGO} />);

    expect(screen.getByText(CATALOGO.options[0]!.label)).toBeInTheDocument();
    expect(screen.queryByText(/quedan en «Otros»/i)).not.toBeInTheDocument();
  });

  it("el copy habla de ESTA hoja, nunca del archivo", () => {
    // Un catálogo puede dejar el stock en su saldo mientras las ventas de la
    // hoja de al lado lo descuentan: decir "este archivo" sería falso para una.
    const { container } = render(<InventoryEffectChoice hoja={VENTAS} />);

    expect(container.textContent).not.toContain("este archivo");
  });
});
