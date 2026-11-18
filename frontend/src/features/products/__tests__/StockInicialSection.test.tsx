/**
 * «Stock inicial» dentro de «Editar producto»: se lee del detalle del producto, la fecha se
 * confirma con el endpoint de siempre, y el costo sólo se pide cuando el stock se cargó sin
 * costo (un costo que ya existe no se corrige acá).
 */
import "@testing-library/jest-dom";
import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { confirmarAperturaDeProducto } from "@/services/dashboard.service";
import { productsService, type AperturaDelProducto } from "@/services/products.service";
import { StockInicialSection } from "../StockInicialSection";

jest.mock("@/services/products.service", () => {
  const actual = jest.requireActual("@/services/products.service");
  return { ...actual, productsService: { getProduct: jest.fn(), setOpeningCost: jest.fn() } };
});
jest.mock("@/services/dashboard.service", () => ({
  confirmarAperturaDeProducto: jest.fn(),
}));

const getProduct = productsService.getProduct as jest.Mock;
const setOpeningCost = productsService.setOpeningCost as jest.Mock;
const confirmar = confirmarAperturaDeProducto as jest.Mock;

const AP: AperturaDelProducto = {
  movement_id: "m1",
  fecha: "2026-11-05",
  confirmada: false,
  origen: "carga",
  con_costo: true,
  costo: "60.00",
  editable: true,
  motivo_no_editable: null,
};

function montar(apertura: AperturaDelProducto | null, primerDia: string | null = null) {
  getProduct.mockResolvedValue({ id: "p1", apertura, primer_dia_de_fuente: primerDia });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <StockInicialSection productId="p1" costoDelProducto={80} />
    </QueryClientProvider>,
  );
}

beforeEach(() => jest.clearAllMocks());

test("dice desde cuándo figura el stock, quién puso la fecha y el costo", async () => {
  montar(AP);
  expect(await screen.findByText(/Figura desde el 05\/11\/2026/)).toBeInTheDocument();
  expect(screen.getByText(/fecha en que se cargó el archivo/)).toBeInTheDocument();
  expect(screen.getByText(/Costo: .*60,00 por unidad/)).toBeInTheDocument();
  // Con costo no se ofrece cargarlo.
  expect(screen.queryByLabelText("Costo del stock inicial")).not.toBeInTheDocument();
});

test("confirmar la fecha usa el endpoint de siempre con la fecha elegida", async () => {
  confirmar.mockResolvedValue({
    aperturas: 1,
    productos: 1,
    ventas_costeadas_antes: 0,
    venta_ids: [],
  });
  montar(AP);
  const input = await screen.findByLabelText(/Desde cuándo tenés el stock/);
  fireEvent.change(input, { target: { value: "2026-08-01T10:00" } });
  fireEvent.click(screen.getByRole("button", { name: "Confirmar fecha" }));
  await waitFor(() => expect(confirmar).toHaveBeenCalledWith("p1", "2026-08-01T10:00"));
});

test("un stock sin costo pide el costo y avisa que sin él la fecha no alcanza", async () => {
  setOpeningCost.mockResolvedValue({
    movement_id: "m1",
    product_id: "p1",
    ventas_sin_costo_desde_la_fecha: 2,
  });
  montar({ ...AP, con_costo: false, costo: null });
  expect(await screen.findByText(/Este stock se cargó sin costo/)).toBeInTheDocument();
  const boton = screen.getByRole("button", { name: "Cargar costo" });
  expect(boton).toBeDisabled();

  fireEvent.change(screen.getByLabelText("Costo del stock inicial"), {
    target: { value: "75,5" },
  });
  fireEvent.click(boton);

  await waitFor(() => expect(setOpeningCost).toHaveBeenCalledWith("p1", "75.50"));
  expect(await screen.findByText(/2 ventas sin costo se costean/)).toBeInTheDocument();
});

test("«Usar el costo del producto» sólo rellena: no guarda solo", async () => {
  montar({ ...AP, con_costo: false, costo: null });
  fireEvent.click(await screen.findByRole("button", { name: /Usar el costo del producto/ }));
  expect(screen.getByLabelText("Costo del stock inicial")).toHaveValue("80");
  expect(setOpeningCost).not.toHaveBeenCalled();
});

test("con varias cargas candidatas la fecha no se edita y se dice por qué", async () => {
  montar({ ...AP, editable: false, motivo_no_editable: "Tiene 2 cargas de stock inicial." });
  expect(await screen.findByText("Tiene 2 cargas de stock inicial.")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Confirmar fecha" })).not.toBeInTheDocument();
});

test("sin stock inicial pero con una compra: lo dice y no ofrece editar", async () => {
  montar(null, "2026-10-03");
  expect(await screen.findByText(/una compra\s+del 03\/10\/2026/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Confirmar fecha" })).not.toBeInTheDocument();
});

test("sin stock inicial ni fuente, o con un servidor viejo, no aparece nada", async () => {
  montar(null, null);
  await waitFor(() => expect(getProduct).toHaveBeenCalled());
  expect(screen.queryByText("Stock inicial")).not.toBeInTheDocument();
});
