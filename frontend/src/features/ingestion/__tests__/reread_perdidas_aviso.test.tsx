import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";

import { RereadPerdidasAviso } from "../RereadPerdidasAviso";

// B·nuevo paso 0 (0c): el freno por una edición a mano dice POR QUÉ frenó y no
// promete que aceptar descarte la edición (`accept_data_loss` la conserva).

const BASE = { to_lose: 0, to_lose_to_others: 0, edited_at_risk: 1, samples: [] };

function aviso(perdida: Parameters<typeof RereadPerdidasAviso>[0]["perdida"]) {
  render(<RereadPerdidasAviso perdida={perdida} acepto={false} onAceptoChange={() => {}} />);
}

describe("RereadPerdidasAviso — edición en riesgo", () => {
  it("muestra el motivo del backend y que aplicar igual NO borra la edición", () => {
    aviso({
      ...BASE,
      motivo: "fila_con_venta_y_gasto",
      motivo_texto: "Tu edición es de una fila que trae una venta y un gasto a la vez.",
    });
    expect(
      screen.getByText(/una fila que trae una venta y un gasto a la vez/),
    ).toBeInTheDocument();
    expect(screen.getByText(/Aplicar igual no borra tu edición/)).toBeInTheDocument();
    expect(screen.getByText(/dejá el archivo como está y revisá el conflicto/)).toBeInTheDocument();
    expect(screen.queryByText(/descart/i)).not.toBeInTheDocument();
  });

  it("con un backend viejo (sin motivo) cae al texto de siempre", () => {
    aviso(BASE);
    expect(screen.getByText(/las filas del archivo se corrieron de lugar/)).toBeInTheDocument();
    expect(screen.getByText(/Aplicar igual no borra tu edición/)).toBeInTheDocument();
  });

  it("una pérdida sin ediciones no habla de ediciones", () => {
    aviso({ ...BASE, edited_at_risk: 0, to_lose: 2 });
    expect(screen.queryByText(/no borra tu edición/)).not.toBeInTheDocument();
    // La casilla de aceptar la pérdida medida sigue.
    expect(
      screen.getByRole("checkbox", { name: /quiero aplicar la relectura igual/i }),
    ).toBeInTheDocument();
  });
});
