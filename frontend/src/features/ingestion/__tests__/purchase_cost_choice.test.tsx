import "@testing-library/jest-dom";
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";

import { PurchaseCostChoice } from "../PurchaseCostChoice";

/**
 * F-H6.c — el selector aparece sólo si la hoja tiene algo que ajustar, y sus
 * defaults no cambian ningún número.
 */
function renderChoice(over: Partial<React.ComponentProps<typeof PurchaseCostChoice>> = {}) {
  const onBase = jest.fn();
  const onLine = jest.fn();
  render(
    <PurchaseCostChoice
      base="monto_incluye"
      lineShipping="gasto_aparte"
      onBaseChange={onBase}
      onLineShippingChange={onLine}
      mostrarAjustes
      mostrarFleteDeLinea
      {...over}
    />,
  );
  return { onBase, onLine };
}

describe("PurchaseCostChoice", () => {
  test("sin columnas de costo no se muestra nada", () => {
    const { container } = render(
      <PurchaseCostChoice
        base="monto_incluye"
        lineShipping="gasto_aparte"
        onBaseChange={jest.fn()}
        onLineShippingChange={jest.fn()}
        mostrarAjustes={false}
        mostrarFleteDeLinea={false}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  test("los dos ejes son independientes: sólo aparece el que corresponde", () => {
    renderChoice({ mostrarFleteDeLinea: false });
    expect(screen.getByText(/ya incluye el descuento/i)).toBeInTheDocument();
    expect(screen.queryByText(/viene asignado a cada línea/i)).not.toBeInTheDocument();
  });

  test("arranca en el default que no toca ningún número", () => {
    renderChoice();
    expect(screen.getByRole("radio", { name: /El monto ya es el final/ })).toBeChecked();
    expect(
      screen.getByRole("radio", { name: /Es un gasto de logística/ }),
    ).toBeChecked();
  });

  test("elegir el bruto avisa al padre sin tocar el otro eje", () => {
    const { onBase, onLine } = renderChoice();
    fireEvent.click(screen.getByRole("radio", { name: /El monto es el bruto/ }));
    expect(onBase).toHaveBeenCalledWith("monto_sin_ajustes");
    expect(onLine).not.toHaveBeenCalled();
  });

  test("capitalizar el flete de línea también", () => {
    const { onBase, onLine } = renderChoice();
    fireEvent.click(screen.getByRole("radio", { name: /Es parte de lo que costó/ }));
    expect(onLine).toHaveBeenCalledWith("al_costo");
    expect(onBase).not.toHaveBeenCalled();
  });

  test("explica la consecuencia, no sólo el nombre de la opción", () => {
    renderChoice();
    expect(screen.getByText(/lo contaría dos veces/i)).toBeInTheDocument();
    expect(screen.getByText(/valuación del stock/i)).toBeInTheDocument();
  });
});
