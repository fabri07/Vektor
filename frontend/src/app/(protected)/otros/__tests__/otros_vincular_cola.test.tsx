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
import { errorDeFecha, VincularColaModal } from "../VincularColaModal";
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
  // La búsqueda arranca vacía: «Coca 500» escondería «Coca-Cola 500 ml».
  expect(screen.getByPlaceholderText("Buscar producto")).toHaveValue("");

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

it("SALE_WITHOUT_PURCHASE en la primera: queda listada y la siguiente crea el alias", async () => {
  svc.linkProduct!
    .mockRejectedValueOnce(
      axiosError(422, { code: "SALE_WITHOUT_PURCHASE", product_name: "Yerba", message: "x" }),
    )
    .mockResolvedValueOnce("ok");
  await abrirCola([venta("r1", "Yerba"), venta("r2", "Yerba")]);
  await elegir("p-yerba");
  fireEvent.click(screen.getByRole("button", { name: "Vincular" }));

  expect(
    await screen.findByText(
      "Ese producto («Yerba») no tiene compra ni stock inicial antes de la fecha de la venta: cargá la compra o el stock inicial primero.",
    ),
  ).toBeInTheDocument();
  expect(svc.linkProduct!.mock.calls.map((c) => c[0])).toEqual(["r1", "r2"]);
  expect(screen.getByText(/Las demás ya quedaron/)).toBeInTheDocument();
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

it("IMPORT_IDENTITY_TAKEN en todas: se listan y el alias no queda creado", async () => {
  svc.linkProduct!.mockRejectedValue(
    axiosError(409, { code: "IMPORT_IDENTITY_TAKEN", message: "otro texto" }),
  );
  await abrirCola([venta("r1", "Yerba"), venta("r2", "Yerba")]);
  await elegir("p-yerba");
  fireEvent.click(screen.getByRole("button", { name: "Vincular" }));

  expect(await screen.findByText(/Ninguna venta se pudo vincular/)).toBeInTheDocument();
  expect(
    screen.getAllByText(
      "Esta venta ya está cargada (otra fila del mismo comprobante/operación ya se registró). Descartala o revisala desde la lista.",
    ),
  ).toHaveLength(2);
  expect(svc.linkProduct).toHaveBeenCalledTimes(2);
});

it("stock insuficiente en una del resto: se lista con lo disponible y no corta las demás", async () => {
  const sinStock = Object.assign(new Error("x"), {
    response: {
      status: 400,
      data: {
        detail: "Stock insuficiente para Yerba: hay 1, la venta pide 2.",
        code: "INSUFFICIENT_STOCK",
        product_id: "p-yerba",
        available: 1,
        requested: 2,
      },
    },
  });
  svc.linkProduct!
    .mockResolvedValueOnce("ok")
    .mockRejectedValueOnce(sinStock)
    .mockResolvedValueOnce("ok");
  await abrirCola([venta("r1", "Yerba"), venta("r2", "Yerba"), venta("r3", "Yerba")]);
  await elegir("p-yerba");
  fireEvent.click(screen.getByRole("button", { name: "Vincular" }));

  expect(
    await screen.findByText("Stock insuficiente para Yerba: hay 1, la venta pide 2."),
  ).toBeInTheDocument();
  expect(svc.linkProduct).toHaveBeenCalledTimes(3);
});

it("stock insuficiente sin texto: arma el mensaje con lo disponible y lo pedido", async () => {
  svc.linkProduct!.mockRejectedValue(
    Object.assign(new Error("x"), {
      response: { status: 400, data: { code: "INSUFFICIENT_STOCK", available: 0, requested: 3 } },
    }),
  );
  await abrirCola([venta("r1", "Yerba")]);
  await elegir("p-yerba");
  fireEvent.click(screen.getByRole("button", { name: "Vincular" }));
  expect(
    await screen.findByText("No hay stock suficiente para esta venta: hay 0 y la venta pide 3."),
  ).toBeInTheDocument();
});

it("en StrictMode, «Vincular» vincula", async () => {
  svc.linkProduct!.mockResolvedValue("ok");
  svc.getPendingSalesWithReference!.mockResolvedValue({
    records: [venta("r1", "Yerba")],
    truncado: false,
  });
  render(
    <React.StrictMode>
      <VincularColaModal onClose={jest.fn()} />
    </React.StrictMode>,
  );
  await screen.findByText(/referencias$/);
  await elegir("p-yerba");
  fireEvent.click(screen.getByRole("button", { name: "Vincular" }));
  expect(await screen.findByText(/Terminaste la cola/)).toBeInTheDocument();
  expect(svc.linkProduct).toHaveBeenCalledTimes(1);
});

it("RECORD_ALREADY_RESOLVED cuenta como resuelta, no como falla", async () => {
  svc.linkProduct!
    .mockRejectedValueOnce(axiosError(409, { code: "RECORD_ALREADY_RESOLVED" }))
    .mockResolvedValueOnce("ok");
  await abrirCola([venta("r1", "Yerba"), venta("r2", "Yerba")]);
  await elegir("p-yerba");
  fireEvent.click(screen.getByRole("button", { name: "Vincular" }));
  expect(await screen.findByText(/Terminaste la cola/)).toBeInTheDocument();
  expect(screen.queryByText(/no se pudieron vincular/)).toBeNull();
});

it("al volver a vincular no repite las ventas ya resueltas", async () => {
  svc.linkProduct!
    .mockRejectedValueOnce(axiosError(409, { code: "RECORD_ALREADY_RESOLVED" }))
    .mockRejectedValueOnce(axiosError(422, { code: "CANTIDAD_REQUERIDA" }))
    .mockResolvedValue("ok");
  await abrirCola([venta("r1", "Queso"), venta("r2", "Queso", { cantidad: "0,5" })]);
  await elegir("p-yerba");
  fireEvent.click(screen.getByRole("button", { name: "Vincular" }));
  fireEvent.change(await screen.findByLabelText("Cantidad vendida"), { target: { value: "1" } });
  fireEvent.click(screen.getByRole("button", { name: "Vincular" }));
  await screen.findByText(/Terminaste la cola/);
  expect(svc.linkProduct!.mock.calls).toEqual([
    ["r1", { product_id: "p-yerba" }],
    ["r2", { product_id: "p-yerba" }],
    ["r2", { product_id: "p-yerba", quantity: 1 }],
  ]);
});

it("la cantidad pedida tiene que ser 1 o más", async () => {
  svc.linkProduct!.mockRejectedValueOnce(axiosError(422, { code: "CANTIDAD_REQUERIDA" }));
  await abrirCola([venta("r1", "Queso", { cantidad: "0,5" })]);
  await elegir("p-yerba");
  fireEvent.click(screen.getByRole("button", { name: "Vincular" }));
  fireEvent.change(await screen.findByLabelText("Cantidad vendida"), { target: { value: "0" } });
  fireEvent.click(screen.getByRole("button", { name: "Vincular" }));
  expect(await screen.findByText("La cantidad tiene que ser 1 o más.")).toBeInTheDocument();
  expect(svc.linkProduct).toHaveBeenCalledTimes(1);
});

it("cerrar a mitad de camino corta lo que falta y avisa al terminar la llamada en vuelo", async () => {
  let soltar: (v: string) => void = () => {};
  svc.linkProduct!.mockImplementationOnce(
    () => new Promise<string>((resolve) => (soltar = resolve)),
  );
  svc.linkProduct!.mockResolvedValue("ok");
  svc.getPendingSalesWithReference!.mockResolvedValue({
    records: [venta("r1", "Yerba"), venta("r2", "Yerba"), venta("r3", "Yerba")],
    truncado: false,
  });
  const trasCerrar = jest.fn();
  const { unmount } = render(
    <VincularColaModal onClose={jest.fn()} onCambiosTrasCerrar={trasCerrar} />,
  );
  await screen.findByText(/referencias$/);
  await elegir("p-yerba");
  fireEvent.click(screen.getByRole("button", { name: "Vincular" }));
  expect(await screen.findByText("Vinculando 1 de 3…")).toBeInTheDocument();
  unmount();
  soltar("ok");
  await waitFor(() => expect(trasCerrar).toHaveBeenCalledTimes(1));
  expect(svc.linkProduct).toHaveBeenCalledTimes(1);
});

it("«Reintentar» pide la cantidad y la manda", async () => {
  svc.linkProduct!
    .mockResolvedValueOnce("ok")
    .mockRejectedValueOnce(axiosError(422, { code: "CANTIDAD_REQUERIDA" }))
    .mockResolvedValueOnce("ok");
  await abrirCola([venta("r1", "Queso"), venta("r2", "Queso", { cantidad: "1,5" })]);
  await elegir("p-yerba");
  fireEvent.click(screen.getByRole("button", { name: "Vincular" }));

  const boton = await screen.findByRole("button", {
    name: /Reintentar la venta del 2026-10-02 por/,
  });
  fireEvent.click(boton);
  expect(await screen.findByText("Ingresá la cantidad.")).toBeInTheDocument();
  expect(svc.linkProduct).toHaveBeenCalledTimes(2);

  fireEvent.change(screen.getByLabelText("Cantidad de la venta del 2026-10-02"), {
    target: { value: "3" },
  });
  fireEvent.click(boton);
  await screen.findByText(/Terminaste la cola/);
  expect(svc.linkProduct).toHaveBeenLastCalledWith("r2", { product_id: "p-yerba", quantity: 3 });
});

it("FECHA_REQUERIDA (V-16): muestra el motivo del backend, pide la fecha y la manda", async () => {
  svc.linkProduct!
    .mockResolvedValueOnce("ok")
    .mockRejectedValueOnce(
      axiosError(422, {
        code: "FECHA_REQUERIDA",
        message: "La fecha «04/03/2026» se puede leer como día/mes o como mes/día.",
      }),
    )
    .mockResolvedValueOnce("ok");
  await abrirCola([venta("r1", "Queso"), venta("r2", "Queso")]);
  await elegir("p-yerba");
  fireEvent.click(screen.getByRole("button", { name: "Vincular" }));

  expect(await screen.findByText(/se puede leer como día\/mes o como mes\/día/)).toBeInTheDocument();
  const boton = screen.getByRole("button", { name: /Reintentar la venta del 2026-10-02 por/ });
  fireEvent.click(boton);
  expect(await screen.findByText("Ingresá la fecha.")).toBeInTheDocument();
  expect(svc.linkProduct).toHaveBeenCalledTimes(2);

  fireEvent.change(screen.getByLabelText("Fecha de la venta del 2026-10-02"), {
    target: { value: "2026-03-04" },
  });
  fireEvent.click(boton);
  await screen.findByText(/Terminaste la cola/);
  expect(svc.linkProduct).toHaveBeenLastCalledWith("r2", {
    product_id: "p-yerba",
    transaction_date: "2026-03-04T00:00:00",
  });
});

it("cantidad y después fecha: el reintento manda las dos y no vuelve a pedir la cantidad", async () => {
  svc.linkProduct!
    .mockResolvedValueOnce("ok")
    .mockRejectedValueOnce(axiosError(422, { code: "CANTIDAD_REQUERIDA" }))
    .mockRejectedValueOnce(
      axiosError(422, { code: "FECHA_REQUERIDA", message: "La fecha «04/03/2026» es ambigua." }),
    )
    .mockResolvedValueOnce("ok");
  await abrirCola([venta("r1", "Queso"), venta("r2", "Queso", { cantidad: "1,5" })]);
  await elegir("p-yerba");
  fireEvent.click(screen.getByRole("button", { name: "Vincular" }));

  const boton = await screen.findByRole("button", {
    name: /Reintentar la venta del 2026-10-02 por/,
  });
  fireEvent.change(screen.getByLabelText("Cantidad de la venta del 2026-10-02"), {
    target: { value: "3" },
  });
  fireEvent.click(boton);
  expect(await screen.findByText("La fecha «04/03/2026» es ambigua.")).toBeInTheDocument();
  expect(svc.linkProduct).toHaveBeenLastCalledWith("r2", { product_id: "p-yerba", quantity: 3 });
  // La cantidad ya escrita sigue a la vista, y aparece la fecha.
  expect(screen.getByLabelText("Cantidad de la venta del 2026-10-02")).toHaveValue(3);
  fireEvent.change(screen.getByLabelText("Fecha de la venta del 2026-10-02"), {
    target: { value: "2026-03-04" },
  });
  fireEvent.click(boton);
  await screen.findByText(/Terminaste la cola/);
  expect(svc.linkProduct).toHaveBeenLastCalledWith("r2", {
    product_id: "p-yerba",
    quantity: 3,
    transaction_date: "2026-03-04T00:00:00",
  });
});

it("errorDeFecha: la fecha indicada no puede ser posterior a hoy", () => {
  const hoy = new Date(2026, 9, 27);
  expect(errorDeFecha("2026-10-27", hoy)).toBeNull();
  expect(errorDeFecha("2026-10-28", hoy)).toBe("La fecha no puede ser posterior a hoy.");
  expect(errorDeFecha("", hoy)).toBe("Ingresá la fecha.");
});

it("al pasar a la referencia siguiente el producto elegido no se arrastra", async () => {
  svc.linkProduct!.mockResolvedValue("ok");
  await abrirCola([venta("r1", "Coca 500"), venta("r2", "Fernet")]);
  await elegir("p-coca");
  fireEvent.click(screen.getByRole("button", { name: "Vincular" }));
  expect(await screen.findByText("«Fernet»")).toBeInTheDocument();
  expect(screen.getByLabelText("Producto vendido")).toHaveValue("");
  expect(screen.getByRole("button", { name: "Vincular" })).toBeDisabled();
});

it("un 422 de validación (lista) no culpa a la fecha o el monto", async () => {
  svc.linkProduct!.mockRejectedValue(axiosError(422, [{ loc: ["body", "amount"], msg: "x" }]));
  await abrirCola([venta("r1", "Yerba")]);
  await elegir("p-yerba");
  fireEvent.click(screen.getByRole("button", { name: "Vincular" }));
  expect(await screen.findByText(/Algún dato de la fila no es válido/)).toBeInTheDocument();
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
