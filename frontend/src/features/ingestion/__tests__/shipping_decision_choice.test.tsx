import "@testing-library/jest-dom";
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";

import { ShippingDecisionChoice } from "../ShippingDecisionChoice";

/**
 * F-H6.b — el control que le da salida al usuario cuando Véktor no puede deducir
 * si un envío repetido es uno o varios.
 *
 * Lo que protegen estos tests es que NO elegir siga siendo posible y siga siendo
 * el default: es lo que evita que Véktor invente un costo.
 */
describe("ShippingDecisionChoice", () => {
  it("ofrece las dos salidas y dice qué hace cada una", () => {
    render(<ShippingDecisionChoice value={null} onChange={jest.fn()} />);

    expect(screen.getByRole("button", { name: /un solo env[íi]o/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /cada fila es un env[íi]o/i })).toBeInTheDocument();
    // El costo de la opción cara se dice con el número, no en abstracto.
    expect(screen.getByText(/\$20\.000/)).toBeInTheDocument();
  });

  it("sin elegir avisa que no se registra ninguno y ofrece la otra salida", () => {
    render(<ShippingDecisionChoice value={null} onChange={jest.fn()} />);

    expect(screen.getByText(/no se registra ninguno/i)).toBeInTheDocument();
    expect(screen.getByText(/mapear la columna del n[úu]mero de comprobante/i)).toBeInTheDocument();
  });

  it("elegir avisa la decisión", () => {
    const onChange = jest.fn();
    render(<ShippingDecisionChoice value={null} onChange={onChange} />);

    fireEvent.click(screen.getByRole("button", { name: /un solo env[íi]o/i }));
    expect(onChange).toHaveBeenCalledWith("una_por_hoja");
  });

  it("volver a tocar la opción activa vuelve al default de no registrar", () => {
    // Sin esto, una vez elegido no habría forma de volver atrás sin recargar — y
    // el default seguro dejaría de ser alcanzable.
    const onChange = jest.fn();
    render(<ShippingDecisionChoice value="una_por_fila" onChange={onChange} />);

    fireEvent.click(screen.getByRole("button", { name: /cada fila es un env[íi]o/i }));
    expect(onChange).toHaveBeenCalledWith(null);
  });

  it("marca la opción activa", () => {
    render(<ShippingDecisionChoice value="una_por_hoja" onChange={jest.fn()} />);

    expect(screen.getByRole("button", { name: /un solo env[íi]o/i })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });
});
