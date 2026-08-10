import "@testing-library/jest-dom";
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";

import { PurchaseCostChoice } from "../PurchaseCostChoice";

/**
 * F-H6.c/d — el selector aparece sólo si la hoja tiene algo que ajustar, y sus
 * defaults no cambian ningún número.
 */
function renderChoice(over: Partial<React.ComponentProps<typeof PurchaseCostChoice>> = {}) {
  const onBase = jest.fn();
  const onShared = jest.fn();
  const onLine = jest.fn();
  render(
    <PurchaseCostChoice
      base="monto_incluye"
      sharedShipping="no_distribuir"
      lineShipping="gasto_aparte"
      onBaseChange={onBase}
      onSharedShippingChange={onShared}
      onLineShippingChange={onLine}
      mostrarAjustes
      mostrarEnvioCompartido
      mostrarFleteDeLinea
      {...over}
    />,
  );
  return { onBase, onShared, onLine };
}

describe("PurchaseCostChoice", () => {
  test("sin columnas de costo no se muestra nada", () => {
    const { container } = render(
      <PurchaseCostChoice
        base="monto_incluye"
        sharedShipping="no_distribuir"
        lineShipping="gasto_aparte"
        onBaseChange={jest.fn()}
        onSharedShippingChange={jest.fn()}
        onLineShippingChange={jest.fn()}
        mostrarAjustes={false}
        mostrarEnvioCompartido={false}
        mostrarFleteDeLinea={false}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  test("los ejes son independientes: sólo aparece el que corresponde", () => {
    renderChoice({ mostrarFleteDeLinea: false, mostrarEnvioCompartido: false });
    expect(screen.getByText(/ya incluye el descuento/i)).toBeInTheDocument();
    expect(screen.queryByText(/viene asignado a cada línea/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/cobra una sola vez/i)).not.toBeInTheDocument();
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

/**
 * F-H6.d — el tercer eje. Existía en el backend y en el tipo del servicio, pero
 * la pantalla nunca lo ofrecía: la distribución del envío compartido era
 * inalcanzable desde la app, igual que `historical_replay` antes de F-H3.e.
 */
describe("PurchaseCostChoice — envío compartido del comprobante", () => {
  test("no aparece cuando el servidor dice que esta hoja no se puede repartir", () => {
    // El padre traduce `puede_distribuir === false` a este flag. Ofrecer el
    // reparto igual dejaría al usuario eligiendo algo que no va a pasar.
    renderChoice({ mostrarEnvioCompartido: false });
    expect(screen.queryByText(/cobra una sola vez/i)).not.toBeInTheDocument();
    expect(
      screen.queryByRole("radio", { name: /Se reparte entre los productos/ }),
    ).not.toBeInTheDocument();
  });

  test("arranca en «no distribuir», que es el que no mueve ningún costo", () => {
    // No se cambia el default: hacerlo alteraría el costo de todos los imports
    // que ya se hicieron con el comportamiento anterior.
    renderChoice();
    expect(
      screen.getByRole("radio", { name: /Queda como gasto aparte/ }),
    ).toBeChecked();
    expect(
      screen.getByRole("radio", { name: /Se reparte entre los productos/ }),
    ).not.toBeChecked();
  });

  test("elegir el reparto avisa al padre sin tocar los otros dos ejes", () => {
    const { onBase, onShared, onLine } = renderChoice();
    fireEvent.click(
      screen.getByRole("radio", { name: /Se reparte entre los productos/ }),
    );
    expect(onShared).toHaveBeenCalledWith("por_subtotal");
    expect(onBase).not.toHaveBeenCalled();
    expect(onLine).not.toHaveBeenCalled();
  });

  test("es un eje aparte del flete de línea: los dos pueden convivir", () => {
    // Un mismo remito puede traer un envío global a repartir y además un flete
    // ya asignado por línea. Son dos plata distintas y dos decisiones distintas.
    renderChoice();
    expect(screen.getByText(/cobra una sola vez/i)).toBeInTheDocument();
    expect(screen.getByText(/viene asignado a cada línea/i)).toBeInTheDocument();
  });
});
