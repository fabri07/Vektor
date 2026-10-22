/**
 * B·8b: la cola «Vincular a producto». Las ventas de «Otros» que dicen qué
 * producto se vendió se resuelven de a una referencia: se elige el producto y
 * todas las ventas con ese texto se vinculan a él (`link-product`, B·8a).
 */
import "@testing-library/jest-dom";
import React from "react";
import { fireEvent, render as rtlRender, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import type { UnclassifiedRecordResponse } from "@/services/others.service";
import { othersService } from "@/services/others.service";
import { VincularColaModal } from "../VincularColaModal";
import OtrosPage from "../page";

jest.mock("@/services/others.service", () => ({
  othersService: {
    getPending: jest.fn(),
    getPendingCounts: jest.fn(),
    getSummary: jest.fn(),
    getPendingSalesWithReference: jest.fn(),
    linkProduct: jest.fn(),
    reclassify: jest.fn(),
    dismiss: jest.fn(),
    bulkImport: jest.fn(),
    resolvePurchase: jest.fn(),
  },
}));

jest.mock("@/services/products.service", () => ({
  productsService: {
    getCategories: jest.fn().mockResolvedValue([]),
    getAllProducts: jest.fn().mockResolvedValue([
      { id: "p-coca", name: "Coca-Cola 500 ml" },
      { id: "p-yerba", name: "Yerba" },
    ]),
  },
}));

jest.mock("@/stores/toastStore", () => ({
  useToastStore: (selector: (s: { add: jest.Mock }) => unknown) => selector({ add: jest.fn() }),
}));

const svc = othersService as unknown as Record<string, jest.Mock>;

function venta(id: string, producto: string, extra: Record<string, string> = {}) {
  return {
    id,
    uploaded_file_id: "f-1",
    source: "ingestion",
    context_label: "Venta sin producto",
    headers: null,
    row_data: { fecha: `2026-10-0${id.slice(-1)}`, monto: "1500", cantidad: "2", producto, ...extra },
    suggested_entity: "sale",
    suggested_category: null,
    suggested_category_label: null,
    match_candidates: null,
    status: "PENDING",
    created_at: "2026-10-11T10:00:00Z",
  } as unknown as UnclassifiedRecordResponse;
}

function axiosError(status: number, detail: unknown) {
  return Object.assign(new Error("x"), { response: { status, data: { detail } } });
}

function render(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return rtlRender(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

async function abrirCola(records: UnclassifiedRecordResponse[]) {
  svc.getPendingSalesWithReference!.mockResolvedValue({ records, truncado: false });
  render(<VincularColaModal onClose={jest.fn()} />);
  await screen.findByText(/referencias$/);
}

async function elegir(productId: string) {
  fireEvent.change(screen.getByPlaceholderText("Buscar producto"), { target: { value: "" } });
  const select = await screen.findByLabelText("Producto vendido");
  await waitFor(() => expect(screen.getByRole("option", { name: "Yerba" })).toBeInTheDocument());
  fireEvent.change(select, { target: { value: productId } });
}

beforeEach(() => {
  jest.clearAllMocks();
});

it("vincula el grupo entero con el mismo producto y pasa a la siguiente referencia", async () => {
  svc.linkProduct!.mockResolvedValue("ok");
  await abrirCola([
    venta("r1", "Coca 500"),
    venta("r2", "coca 500 "),
    venta("r3", "Coca 500"),
    venta("r4", "Yerba suelta"),
  ]);
  expect(screen.getByText("1 de 2 referencias")).toBeInTheDocument();
  expect(screen.getByText("«Coca 500»")).toBeInTheDocument();
  expect(screen.getByText("La usan 3 ventas")).toBeInTheDocument();
  // La búsqueda arranca con lo que decía el archivo.
  expect(screen.getByPlaceholderText("Buscar producto")).toHaveValue("Coca 500");

  await elegir("p-coca");
  fireEvent.click(screen.getByRole("button", { name: "Vincular" }));

  await screen.findByText("2 de 2 referencias");
  expect(svc.linkProduct!.mock.calls).toEqual([
    ["r1", { product_id: "p-coca" }],
    ["r2", { product_id: "p-coca" }],
    ["r3", { product_id: "p-coca" }],
  ]);
  expect(screen.getByText(/Se vincularon 3 venta\(s\) de «Coca 500» a «Coca-Cola 500 ml»/)).toBeInTheDocument();
});

it("una falla del resto del grupo se muestra sin cortar las demás", async () => {
  svc.linkProduct!
    .mockResolvedValueOnce("ok")
    .mockRejectedValueOnce(
      axiosError(422, { code: "SALE_WITHOUT_PURCHASE", product_name: "Yerba" }),
    )
    .mockResolvedValueOnce("ok");
  await abrirCola([venta("r1", "Yerba"), venta("r2", "Yerba"), venta("r3", "Yerba")]);
  await elegir("p-yerba");
  fireEvent.click(screen.getByRole("button", { name: "Vincular" }));

  expect(await screen.findByText(/no se pudieron vincular/)).toBeInTheDocument();
  expect(svc.linkProduct).toHaveBeenCalledTimes(3);
  expect(screen.getByRole("button", { name: "Siguiente referencia" })).toBeInTheDocument();
});

it("Saltear pasa a la referencia siguiente sin llamar al backend", async () => {
  await abrirCola([venta("r1", "Coca 500"), venta("r2", "Coca 500"), venta("r3", "Fernet")]);
  expect(screen.getByText("«Coca 500»")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Saltear" }));
  expect(await screen.findByText("«Fernet»")).toBeInTheDocument();
  expect(screen.getByText("2 de 2 referencias")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Saltear" }));
  expect(await screen.findByText(/Terminaste la cola/)).toBeInTheDocument();
  expect(svc.linkProduct).not.toHaveBeenCalled();
});

it("SALE_WITHOUT_PURCHASE en la primera: dice qué cargar y no sigue con el resto", async () => {
  svc.linkProduct!.mockRejectedValue(
    axiosError(422, { code: "SALE_WITHOUT_PURCHASE", product_name: "Yerba", message: "x" }),
  );
  await abrirCola([venta("r1", "Yerba"), venta("r2", "Yerba")]);
  await elegir("p-yerba");
  fireEvent.click(screen.getByRole("button", { name: "Vincular" }));

  expect(
    await screen.findByText(
      "Ese producto («Yerba») no tiene compra ni stock inicial antes de la fecha de la venta: cargá la compra o el stock inicial primero.",
    ),
  ).toBeInTheDocument();
  expect(svc.linkProduct).toHaveBeenCalledTimes(1);
  expect(screen.getByText("1 de 1 referencias")).toBeInTheDocument();
});

it("CANTIDAD_REQUERIDA: pide la cantidad y reintenta con ella", async () => {
  svc.linkProduct!
    .mockRejectedValueOnce(axiosError(422, { code: "CANTIDAD_REQUERIDA" }))
    .mockResolvedValue("ok");
  await abrirCola([venta("r1", "Queso", { cantidad: "1,5" }), venta("r2", "Queso")]);
  await elegir("p-yerba");
  fireEvent.click(screen.getByRole("button", { name: "Vincular" }));

  const cantidad = await screen.findByLabelText("Cantidad vendida");
  expect(screen.getByText(/no es un número entero/)).toBeInTheDocument();
  fireEvent.change(cantidad, { target: { value: "2" } });
  fireEvent.click(screen.getByRole("button", { name: "Vincular" }));

  await screen.findByText(/Terminaste la cola/);
  expect(svc.linkProduct!.mock.calls).toEqual([
    ["r1", { product_id: "p-yerba" }],
    ["r1", { product_id: "p-yerba", quantity: 2 }],
    ["r2", { product_id: "p-yerba" }],
  ]);
});

it("409 ALIAS_OTRO_PRODUCTO: dice de qué producto es ese nombre", async () => {
  svc.linkProduct!.mockRejectedValue(
    axiosError(409, {
      code: "ALIAS_OTRO_PRODUCTO",
      message: "«Coca 500» ya está vinculado a «Coca-Cola 500 ml». Un mismo nombre del archivo no puede ser de dos productos: elegí ése o cambiá el vínculo.",
    }),
  );
  await abrirCola([venta("r1", "Coca 500"), venta("r2", "Coca 500")]);
  await elegir("p-yerba");
  fireEvent.click(screen.getByRole("button", { name: "Vincular" }));

  expect(await screen.findByText(/ya está vinculado a «Coca-Cola 500 ml»/)).toBeInTheDocument();
  expect(svc.linkProduct).toHaveBeenCalledTimes(1);
});

it("sin referencias (ninguna venta dice el producto) muestra el mensaje vacío", async () => {
  svc.getPendingSalesWithReference!.mockResolvedValue({ records: [], truncado: false });
  render(<VincularColaModal onClose={jest.fn()} />);
  expect(await screen.findByText(/No hay ventas pendientes que digan qué producto/)).toBeInTheDocument();
});

it("avisa cuando la cola se cortó en el tope", async () => {
  svc.getPendingSalesWithReference!.mockResolvedValue({
    records: [venta("r1", "Coca 500")],
    truncado: true,
  });
  render(<VincularColaModal onClose={jest.fn()} />);
  expect(
    await screen.findByText(/Se muestran las referencias de las primeras 5\.000 ventas pendientes/),
  ).toBeInTheDocument();
});

describe("botón «Vincular ventas a productos» en /otros", () => {
  const otraFila = { ...venta("r9", "Coca 500"), suggested_entity: "expense" };

  beforeEach(() => {
    svc.getPending!.mockResolvedValue([otraFila]);
    svc.getPendingCounts!.mockResolvedValue({ pending: 1, pendingSuggested: 1 });
  });

  it("no aparece sin ventas pendientes, ni trae las filas", async () => {
    svc.getSummary!.mockResolvedValue([
      { group_key: "g1", suggested_entity: "expense", status: "PENDING", count: 4 },
    ]);
    render(<OtrosPage />);
    await screen.findByRole("button", { name: /importar todo lo sugerido/i });
    await waitFor(() => expect(svc.getSummary).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: /vincular ventas a productos/i })).toBeNull();
    expect(svc.getPendingSalesWithReference).not.toHaveBeenCalled();
  });

  it("aparece con ventas pendientes y recién al abrirse trae la cola", async () => {
    svc.getSummary!.mockResolvedValue([
      { group_key: "g1", suggested_entity: "sale", status: "PENDING", count: 2 },
    ]);
    svc.getPendingSalesWithReference!.mockResolvedValue({
      records: [venta("r1", "Coca 500")],
      truncado: false,
    });
    render(<OtrosPage />);
    const boton = await screen.findByRole("button", { name: /vincular ventas a productos/i });
    expect(svc.getPendingSalesWithReference).not.toHaveBeenCalled();
    fireEvent.click(boton);
    expect(await screen.findByText("«Coca 500»")).toBeInTheDocument();
    expect(svc.getPendingSalesWithReference).toHaveBeenCalledWith(5000);
  });
});
