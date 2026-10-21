/**
 * B·5d: la tabla de stock inicial a mano. Dos pasos (revisar → confirmar), una
 * elección obligatoria por fila que se parece a un producto existente, y una
 * revisión que se invalida al editar la tabla o al cambiar los productos.
 */
import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { AxiosError, AxiosHeaders } from "axios";

import { StockInicialTabla } from "../StockInicialTabla";
import { productsService } from "@/services/products.service";

jest.mock("@/services/products.service", () => ({
  productsService: {
    previewInitialStockBatch: jest.fn(),
    confirmInitialStockBatch: jest.fn(),
  },
}));

const mockPreview = productsService.previewInitialStockBatch as jest.Mock;
const mockConfirm = productsService.confirmInitialStockBatch as jest.Mock;

function error409(detail: Record<string, unknown>) {
  return new AxiosError("conflict", "409", undefined, undefined, {
    status: 409,
    statusText: "Conflict",
    headers: {},
    config: { headers: new AxiosHeaders() },
    data: { detail },
  });
}

function escribir(label: string, valor: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value: valor } });
}

function completarFila() {
  escribir("Nombre, fila 1", "Vela aromática");
  escribir("Precio de venta, fila 1", "3000");
  escribir("Costo, fila 1", "1200");
  escribir("Stock, fila 1", "10");
  fireEvent.change(screen.getByLabelText("¿Desde qué fecha tenés este stock?"), {
    target: { value: "2026-08-01" },
  });
}

const SIN_COINCIDENCIAS = {
  filas: [{ fila: 0, errores: [], coincidencias: [] }],
  preview_token: "tok-1",
};

const CON_COINCIDENCIA = {
  filas: [
    {
      fila: 0,
      errores: [],
      coincidencias: [
        { product_id: "p-vela", nombre: "Vela", por: "nombre", necesita_stock_inicial: true },
        { product_id: "p-otra", nombre: "Vela grande", por: "nombre", necesita_stock_inicial: false },
      ],
    },
  ],
  preview_token: "tok-2",
};

const confirmar = () => screen.getByRole("button", { name: "Confirmar" });

beforeEach(() => {
  jest.clearAllMocks();
});

test("flujo feliz: revisa, confirma con fecha y clave, y muestra el resumen", async () => {
  mockPreview.mockResolvedValue(SIN_COINCIDENCIAS);
  mockConfirm.mockResolvedValue({
    filas: [{ fila: 0, accion: "crear", product_id: "p1", movement_id: "m1", campos_no_aplicados: [] }],
    creados: 1,
    con_apertura: 0,
  });
  const onDone = jest.fn();
  render(<StockInicialTabla onDone={onDone} />);
  expect(screen.getByText(/compraron vos o tus socios/)).toBeInTheDocument();
  // Sin default a hoy.
  expect(screen.getByLabelText("¿Desde qué fecha tenés este stock?")).toHaveValue("");
  completarFila();
  expect(confirmar()).toBeDisabled();

  fireEvent.click(screen.getByRole("button", { name: "Revisar" }));
  await waitFor(() => expect(confirmar()).toBeEnabled());
  expect(mockPreview).toHaveBeenCalledWith([
    { nombre: "Vela aromática", sale_price_ars: 3000, unit_cost_ars: 1200, stock_units: 10 },
  ]);

  fireEvent.click(confirmar());
  await waitFor(() => expect(onDone).toHaveBeenCalled());
  const [body, clave] = mockConfirm.mock.calls[0];
  expect(body).toEqual({
    filas: [{ nombre: "Vela aromática", sale_price_ars: 3000, unit_cost_ars: 1200, stock_units: 10 }],
    elecciones: [],
    preview_token: "tok-1",
    fecha: "2026-08-01T00:00:00",
  });
  expect(typeof clave).toBe("string");
  expect(clave.length).toBeGreaterThan(0);
  expect(await screen.findByText(/1 producto\(s\) nuevo\(s\)/)).toBeInTheDocument();
});

test("con coincidencia, Confirmar espera la elección y la elección viaja", async () => {
  mockPreview.mockResolvedValue(CON_COINCIDENCIA);
  mockConfirm.mockResolvedValue({
    filas: [
      {
        fila: 0,
        accion: "apertura_en",
        product_id: "p-vela",
        movement_id: "m1",
        campos_no_aplicados: ["sale_price_ars", "nombre"],
      },
    ],
    creados: 0,
    con_apertura: 1,
  });
  render(<StockInicialTabla />);
  completarFila();
  fireEvent.click(screen.getByRole("button", { name: "Revisar" }));

  const grupo = await screen.findByRole("radiogroup", { name: /fila 1/ });
  // La que ya tiene stock inicial se muestra, pero no se puede elegir.
  expect(within(grupo).getByText(/«Vela grande»: ya tiene stock inicial/)).toBeInTheDocument();
  expect(within(grupo).queryByLabelText(/Es «Vela grande»/)).not.toBeInTheDocument();
  expect(confirmar()).toBeDisabled();

  fireEvent.click(within(grupo).getByLabelText(/Es «Vela»/));
  expect(confirmar()).toBeEnabled();
  fireEvent.click(confirmar());

  await waitFor(() => expect(mockConfirm).toHaveBeenCalled());
  expect(mockConfirm.mock.calls[0][0].elecciones).toEqual([
    { fila: 0, accion: "apertura_en", product_id: "p-vela" },
  ]);
  expect(await screen.findByText(/no se cambió: precio, nombre/)).toBeInTheDocument();
});

test("una coincidencia por SKU no deja elegir «producto nuevo»", async () => {
  mockPreview.mockResolvedValue({
    filas: [
      {
        fila: 0,
        errores: [],
        coincidencias: [
          { product_id: "p-vela", nombre: "Vela", por: "sku", necesita_stock_inicial: true },
        ],
      },
    ],
    preview_token: "tok",
  });
  render(<StockInicialTabla />);
  completarFila();
  fireEvent.click(screen.getByRole("button", { name: "Revisar" }));
  const grupo = await screen.findByRole("radiogroup", { name: /fila 1/ });
  expect(within(grupo).getByLabelText(/Es un producto nuevo/)).toBeDisabled();
});

test("BATCH_CHANGED reemplaza la revisión y pide revisar de nuevo", async () => {
  mockPreview.mockResolvedValue(SIN_COINCIDENCIAS);
  mockConfirm.mockRejectedValue(
    error409({
      code: "BATCH_CHANGED",
      message: "cambió",
      preview: CON_COINCIDENCIA,
    }),
  );
  render(<StockInicialTabla />);
  completarFila();
  fireEvent.click(screen.getByRole("button", { name: "Revisar" }));
  await waitFor(() => expect(confirmar()).toBeEnabled());
  fireEvent.click(confirmar());

  // Ahora la fila se parece a un producto: hay que elegir antes de confirmar.
  expect(await screen.findByRole("radiogroup", { name: /fila 1/ })).toBeInTheDocument();
  expect(screen.getByText(/cambiaron desde que revisaste la tabla/)).toBeInTheDocument();
  expect(confirmar()).toBeDisabled();

  fireEvent.click(screen.getByLabelText(/Es «Vela»/));
  mockConfirm.mockResolvedValue({ filas: [], creados: 0, con_apertura: 1 });
  fireEvent.click(confirmar());
  await waitFor(() => expect(mockConfirm).toHaveBeenCalledTimes(2));
  // Viaja el token del preview nuevo, con otra clave de idempotencia.
  expect(mockConfirm.mock.calls[1][0].preview_token).toBe("tok-2");
  expect(mockConfirm.mock.calls[1][1]).not.toBe(mockConfirm.mock.calls[0][1]);
});

test("editar una fila después de revisar invalida la revisión", async () => {
  mockPreview.mockResolvedValue(SIN_COINCIDENCIAS);
  render(<StockInicialTabla />);
  completarFila();
  fireEvent.click(screen.getByRole("button", { name: "Revisar" }));
  await waitFor(() => expect(confirmar()).toBeEnabled());

  escribir("Stock, fila 1", "12");
  expect(confirmar()).toBeDisabled();
  expect(screen.getByText(/volvé a revisarla/)).toBeInTheDocument();
  expect(mockConfirm).not.toHaveBeenCalled();
});

test("PRODUCTS_BEING_CREATED avisa que se reintente", async () => {
  mockPreview.mockResolvedValue(SIN_COINCIDENCIAS);
  mockConfirm.mockRejectedValue(error409({ code: "PRODUCTS_BEING_CREATED", message: "x" }));
  render(<StockInicialTabla />);
  completarFila();
  fireEvent.click(screen.getByRole("button", { name: "Revisar" }));
  await waitFor(() => expect(confirmar()).toBeEnabled());
  fireEvent.click(confirmar());
  expect(await screen.findByText(/Probá de nuevo en un momento/)).toBeInTheDocument();
});

test("una fila sin costo no se revisa", () => {
  render(<StockInicialTabla />);
  escribir("Nombre, fila 1", "Vela");
  escribir("Precio de venta, fila 1", "3000");
  escribir("Stock, fila 1", "10");
  fireEvent.click(screen.getByRole("button", { name: "Revisar" }));
  expect(screen.getByText(/El costo es obligatorio/)).toBeInTheDocument();
  expect(mockPreview).not.toHaveBeenCalled();
});
