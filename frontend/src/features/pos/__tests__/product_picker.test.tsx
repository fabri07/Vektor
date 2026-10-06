/**
 * El buscador de la caja no dice «Sin resultados» mientras todavía busca.
 *
 * Hasta 2026-10 lo decía durante los segundos que tardaba el servidor, y el
 * cajero cerraba el buscador creyendo que el producto no existía.
 */
import { act, fireEvent, render, screen } from "@testing-library/react";

import { ProductPicker } from "@/features/pos/ProductPicker";
import type { PosProduct } from "@/lib/pos/cart";
import { posService } from "@/services/pos.service";

jest.mock("@/services/pos.service", () => ({
  posService: { searchCatalog: jest.fn() },
}));

const svc = posService as jest.Mocked<typeof posService>;

const cepillo: PosProduct = {
  id: "p9",
  name: "Cepillo lavaplatos",
  sale_price_ars: "2500.00",
  sale_unit: "unit",
  base_units_per_sale_unit: 1,
  stock_units: 4,
};

beforeEach(() => {
  jest.useFakeTimers();
  svc.searchCatalog.mockReset();
});

afterEach(() => {
  jest.useRealTimers();
});

function escribir(texto: string) {
  render(<ProductPicker picker={{ mode: "search" }} onPick={jest.fn()} onClose={jest.fn()} />);
  fireEvent.change(screen.getByLabelText("Buscar por nombre"), { target: { value: texto } });
}

it("mientras el servidor no contestó dice «Buscando…», no «Sin resultados»", async () => {
  let contestar: (items: PosProduct[]) => void = () => {};
  svc.searchCatalog.mockReturnValue(new Promise((r) => (contestar = r)));
  escribir("cepillo lavaplatos");
  await act(async () => {
    jest.advanceTimersByTime(300);
  });
  expect(screen.getByText("Buscando…")).toBeInTheDocument();
  expect(screen.queryByText("Sin resultados.")).not.toBeInTheDocument();

  await act(async () => {
    contestar([cepillo]);
  });
  expect(screen.getByText("Cepillo lavaplatos")).toBeInTheDocument();
  expect(screen.queryByText("Buscando…")).not.toBeInTheDocument();
});

it("«Sin resultados» sólo cuando la búsqueda terminó vacía", async () => {
  svc.searchCatalog.mockResolvedValue([]);
  escribir("inexistente");
  // Antes de que salga la búsqueda (debounce) tampoco se afirma nada.
  expect(screen.queryByText("Sin resultados.")).not.toBeInTheDocument();
  await act(async () => {
    jest.advanceTimersByTime(300);
  });
  expect(screen.getByText("Sin resultados.")).toBeInTheDocument();
  expect(screen.queryByText("Buscando…")).not.toBeInTheDocument();
});

it("al abrir, el foco está en el campo de búsqueda y no en la X del modal", () => {
  // Lo que el cajero tipea tiene que ir al buscador: con el foco en la X las
  // teclas terminaban en otro campo de la caja.
  render(<ProductPicker picker={{ mode: "search" }} onPick={jest.fn()} onClose={jest.fn()} />);
  expect(screen.getByLabelText("Buscar por nombre")).toHaveFocus();
});
