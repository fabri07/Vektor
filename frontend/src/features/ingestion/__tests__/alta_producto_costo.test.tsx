/**
 * M·P2c: el alta de producto con stock pide el precio de compra (obligatorio)
 * y si incluye el envío (opcional); sin stock, el costo sigue siendo opcional.
 */
import "@testing-library/jest-dom";
import { fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ManualEntrySection } from "../ManualEntrySection";

const mockSubmit = jest.fn();
jest.mock("../useOfflineSubmit", () => ({
  useOfflineSubmit: () => ({ submit: mockSubmit }),
  useOfflineQueueCount: () => 0,
  useOfflineFailedCount: () => 0,
  useOnlineStatus: () => true,
}));
jest.mock("../FailedQueuePanel", () => ({ FailedQueuePanel: () => null }));
jest.mock("@/services/products.service", () => ({
  productsService: {
    getAllProducts: jest.fn().mockResolvedValue([]),
    getCategories: jest.fn().mockResolvedValue([]),
  },
}));

function abrirAlta() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <ManualEntrySection />
    </QueryClientProvider>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Alta de producto" }));
}

function escribir(label: RegExp, valor: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value: valor } });
}

beforeEach(() => {
  jest.clearAllMocks();
  mockSubmit.mockResolvedValue(undefined);
});

test("con stock y sin precio de compra no se envía", () => {
  abrirAlta();
  escribir(/Nombre del producto/, "Vela");
  escribir(/Precio de venta/, "300");
  escribir(/Stock inicial/, "10");
  fireEvent.click(screen.getByRole("button", { name: "Dar de alta" }));

  expect(screen.getByText(/hace falta el precio de compra/)).toBeInTheDocument();
  expect(mockSubmit).not.toHaveBeenCalled();
});

test("con stock y precio viaja el envío elegido", () => {
  abrirAlta();
  escribir(/Nombre del producto/, "Vela");
  escribir(/Precio de venta/, "300");
  escribir(/Stock inicial/, "10");
  escribir(/^Precio de compra/, "80");
  escribir(/incluye el envío/, "no_aplica");
  fireEvent.click(screen.getByRole("button", { name: "Dar de alta" }));

  expect(mockSubmit).toHaveBeenCalledWith(
    "product",
    expect.objectContaining({ stock_units: 10, unit_cost_ars: 80, costo_envio: "no_aplica" }),
    expect.anything(),
  );
});

test("sin stock el precio de compra sigue siendo opcional y no se pregunta el envío", () => {
  abrirAlta();
  escribir(/Nombre del producto/, "Vela");
  escribir(/Precio de venta/, "300");
  expect(screen.queryByLabelText(/incluye el envío/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Dar de alta" }));

  const payload = mockSubmit.mock.calls[0][1];
  expect(payload).toMatchObject({ stock_units: 0, unit_cost_ars: null });
  expect(payload).not.toHaveProperty("costo_envio");
});
