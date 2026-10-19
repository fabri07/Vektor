/**
 * M·2d: el aviso pide la fecha del stock inicial. Nada se confirma solo: sin
 * fecha (o con una futura) el botón no manda nada; con fecha, va por archivo o
 * por producto, y dice qué ventas quedaron antes con su costo intacto.
 */
import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { AperturasPorConfirmarAviso, ConfirmarAperturaDeArchivo } from "../AperturasPorConfirmar";
import type { AperturasPorConfirmar } from "@/types/api";

const mockArchivo = jest.fn();
const mockProducto = jest.fn();
jest.mock("@/services/dashboard.service", () => ({
  confirmarAperturaDeArchivo: (...a: unknown[]) => mockArchivo(...a),
  confirmarAperturaDeProducto: (...a: unknown[]) => mockProducto(...a),
}));

const DATOS: AperturasPorConfirmar = {
  sin_confirmar: 3,
  por_archivo: [
    { upload_id: "u1", archivo: "catalogo.xlsx", aperturas: 2, productos: 2, fecha_actual: "2026-10-01" },
  ],
  manuales: [{ product_id: "p1", producto: "Yerba", fecha_actual: "2026-10-05" }],
  ventas_sin_costo_antes: 4,
  ventas_sin_costo_con_fuente: 0,
  ventas_costeadas_antes: 0,
};

function pintar(datos: AperturasPorConfirmar = DATOS) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <AperturasPorConfirmarAviso datos={datos} />
    </QueryClientProvider>,
  );
}

beforeEach(() => jest.clearAllMocks());

test("sin fecha el botón no confirma nada", () => {
  pintar();
  expect(screen.getByText(/4 ventas son anteriores/)).toBeInTheDocument();
  const [boton] = screen.getAllByRole("button", { name: "Confirmar fecha" });
  expect(boton).toBeDisabled();
});

test("una fecha futura no se puede confirmar", () => {
  pintar();
  fireEvent.change(screen.getByLabelText(/stock de catalogo.xlsx/), {
    target: { value: "2999-01-01T09:00" },
  });
  expect(screen.getByText(/no puede ser futura/)).toBeInTheDocument();
  expect(screen.getAllByRole("button", { name: "Confirmar fecha" })[0]).toBeDisabled();
});

test("por archivo manda la fecha y avisa las ventas costeadas que quedan antes", async () => {
  mockArchivo.mockResolvedValue({ aperturas: 2, productos: 2, ventas_costeadas_antes: 1, venta_ids: ["v1"] });
  pintar();
  fireEvent.change(screen.getByLabelText(/stock de catalogo.xlsx/), {
    target: { value: "2026-08-01T09:00" },
  });
  fireEvent.click(screen.getAllByRole("button", { name: "Confirmar fecha" })[0]!);

  await waitFor(() => expect(mockArchivo).toHaveBeenCalledWith("u1", "2026-08-01T09:00", false));
  expect(await screen.findByText(/1 venta quedó antes de esa fecha/)).toBeInTheDocument();
  expect(mockProducto).not.toHaveBeenCalled();
});

test("lo cargado a mano se confirma por producto", async () => {
  mockProducto.mockResolvedValue({ aperturas: 1, productos: 1, ventas_costeadas_antes: 0, venta_ids: [] });
  pintar();
  fireEvent.change(screen.getByLabelText(/stock de Yerba/), {
    target: { value: "2026-07-15T10:00" },
  });
  fireEvent.click(screen.getAllByRole("button", { name: "Confirmar fecha" })[1]!);

  await waitFor(() => expect(mockProducto).toHaveBeenCalledWith("p1", "2026-07-15T10:00"));
  expect(await screen.findByText(/Listo: 1 producto con la fecha nueva\.$/)).toBeInTheDocument();
});

test("sin nada por confirmar no pide fecha y dice las ventas que siguen sin costo", () => {
  pintar({
    ...DATOS,
    sin_confirmar: 0,
    por_archivo: [],
    manuales: [],
    ventas_sin_costo_antes: 0,
    ventas_sin_costo_con_fuente: 2,
  });
  expect(screen.queryByText(/Indicá desde cuándo/)).not.toBeInTheDocument();
  expect(screen.getByText(/2 ventas quedaron sin costo/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Confirmar fecha" })).not.toBeInTheDocument();
});

test("un 409 muestra el motivo del servidor", async () => {
  mockProducto.mockRejectedValue({
    response: { status: 409, data: { detail: { code: "OPENING_AMBIGUOUS", message: "No se puede elegir cuál corregir." } } },
  });
  pintar();
  fireEvent.change(screen.getByLabelText(/stock de Yerba/), {
    target: { value: "2026-07-15T10:00" },
  });
  fireEvent.click(screen.getAllByRole("button", { name: "Confirmar fecha" })[1]!);
  expect(await screen.findByText("No se puede elegir cuál corregir.")).toBeInTheDocument();
});


function pintarArchivo(props: { conFechaDeFila: number; pendientes: number; fechaSugerida?: string }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <ul>
        <ConfirmarAperturaDeArchivo uploadId="u1" titulo="stock.xlsx" detalle="" {...props} />
      </ul>
    </QueryClientProvider>,
  );
}

test("M·2d-1b: la sugerencia sólo llena el campo, no confirma", () => {
  pintarArchivo({ conFechaDeFila: 0, pendientes: 2, fechaSugerida: "2026-07-01" });
  const boton = screen.getByRole("button", { name: "Confirmar fecha" });
  expect(boton).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Usar 01/07/2026" }));
  expect(screen.getByLabelText(/stock de stock.xlsx/)).toHaveValue("2026-07-01T00:00");
  expect(mockArchivo).not.toHaveBeenCalled();
  expect(boton).toBeEnabled();
});

test("M·2d-1b: con todo fechado en las filas, aceptar alcanza sin otra fecha", async () => {
  mockArchivo.mockResolvedValue({ aperturas: 2, productos: 2, ventas_costeadas_antes: 0, venta_ids: [] });
  pintarArchivo({ conFechaDeFila: 2, pendientes: 0 });
  const boton = screen.getByRole("button", { name: "Confirmar fecha" });
  expect(boton).toBeDisabled();
  fireEvent.click(screen.getByLabelText(/su\s+propia fecha en el archivo/));
  expect(boton).toBeEnabled();
  fireEvent.click(boton);
  await waitFor(() => expect(mockArchivo).toHaveBeenCalledWith("u1", null, true));
  // Confirmado: no se puede volver a mandar desde el mismo bloque.
  await waitFor(() => expect(boton).toBeDisabled());
});

test("M·2d-1b: sin la casilla, avisa que la fecha pisa las de las filas", () => {
  pintarArchivo({ conFechaDeFila: 1, pendientes: 1 });
  fireEvent.change(screen.getByLabelText(/stock de stock.xlsx/), {
    target: { value: "2026-07-01T09:00" },
  });
  expect(screen.getByText(/reemplaza también la de las filas/)).toBeInTheDocument();
});

test("M·2d-1b: con filas sin fecha, aceptar las demás no alcanza", () => {
  pintarArchivo({ conFechaDeFila: 1, pendientes: 2 });
  fireEvent.click(screen.getByLabelText(/su\s+propia fecha en el archivo/));
  expect(screen.getByRole("button", { name: "Confirmar fecha" })).toBeDisabled();
  expect(screen.getByText(/los 2 que no traen/)).toBeInTheDocument();
});
