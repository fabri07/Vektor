/**
 * V-24: una venta fiada que el import mandó a «Otros» por no tener cliente se
 * resuelve eligiendo a quién se le fió. El modal arranca con la forma de pago
 * de la fila (no «Efectivo»: el fiado entraría a caja como cobrado), pide el
 * cliente y lo manda; cambiar el medio a propósito viaja como marca explícita.
 */
import React from "react";
import { fireEvent, render as rtlRender, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import type { UnclassifiedRecordResponse } from "@/services/others.service";
import { ReclassifyModal } from "../ReclassifyModal";

jest.mock("@/services/products.service", () => ({
  productsService: {
    getAllProducts: jest.fn().mockResolvedValue([{ id: "p-yerba", name: "Yerba" }]),
  },
}));

jest.mock("@/services/customers.service", () => ({
  customersService: {
    getAllCustomers: jest.fn().mockResolvedValue([
      { id: "c-ana", name: "Ana", last_name: "Pérez", is_sentinel: false },
      { id: "c-pedro", name: "Pedro Gómez", last_name: null, is_sentinel: false },
    ]),
  },
}));

function render(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return rtlRender(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function fila(extra: Record<string, unknown> = {}): UnclassifiedRecordResponse {
  return {
    id: "r-1",
    uploaded_file_id: null,
    source: "ingestion",
    context_label: "Fiado sin cliente: indicá a quién se le fió",
    headers: null as unknown as string[],
    row_data: {
      fecha: "2026-10-10",
      monto: "2000.00",
      cantidad: "2",
      producto: "Yerba",
      medio_de_pago: "account",
      ...extra,
    },
    suggested_entity: "sale",
    suggested_category: null,
    suggested_category_label: null,
    match_candidates: null as unknown as [],
    status: "PENDING",
    created_at: "2026-10-11T10:00:00Z",
  } as UnclassifiedRecordResponse;
}

function abrir(record: UnclassifiedRecordResponse, onSave: jest.Mock) {
  render(
    <ReclassifyModal
      record={record}
      productCategories={[]}
      saving={false}
      onClose={jest.fn()}
      onSave={onSave}
      onLink={jest.fn()}
      onResolvePurchase={jest.fn()}
    />,
  );
}

async function elegirProducto() {
  const select = await screen.findByLabelText("Producto vendido");
  await waitFor(() => expect(screen.getByRole("option", { name: "Yerba" })).toBeInTheDocument());
  fireEvent.change(select, { target: { value: "p-yerba" } });
}

const importar = () =>
  fireEvent.click(screen.getByRole("button", { name: /importar|guardar|clasificar/i }));

it("arranca con la forma de pago de la fila, no con Efectivo", async () => {
  abrir(fila(), jest.fn());
  expect(screen.getByLabelText("Forma de pago")).toHaveValue("account");
  expect(await screen.findByLabelText("Cliente al que se le fió")).toBeInTheDocument();
});

it("sin cliente no se manda y dice por qué", async () => {
  const onSave = jest.fn();
  abrir(fila(), onSave);
  await elegirProducto();
  await waitFor(() => expect(screen.getByRole("option", { name: "Ana Pérez" })).toBeInTheDocument());
  importar();
  expect(await screen.findByText(/El fiado necesita un cliente registrado/)).toBeInTheDocument();
  expect(onSave).not.toHaveBeenCalled();
});

it("manda el cliente elegido, como fiado y sin marca de cambio", async () => {
  const onSave = jest.fn();
  abrir(fila(), onSave);
  await elegirProducto();
  const cliente = await screen.findByLabelText("Cliente al que se le fió");
  await waitFor(() => expect(screen.getByRole("option", { name: "Ana Pérez" })).toBeInTheDocument());
  fireEvent.change(cliente, { target: { value: "c-ana" } });
  importar();
  await waitFor(() =>
    expect(onSave).toHaveBeenCalledWith(
      "sale",
      expect.objectContaining({ payment_method: "account", customer_id: "c-ana", amount: 2000 }),
    ),
  );
  expect(onSave.mock.calls[0]).toHaveLength(2);
});

it("el cliente que nombraba el archivo queda sugerido", async () => {
  abrir(fila({ cliente: "pedro gomez" }), jest.fn());
  const cliente = await screen.findByLabelText("Cliente al que se le fió");
  await waitFor(() => expect(cliente).toHaveValue("c-pedro"));
  expect(screen.getByText(/El archivo decía: «pedro gomez»/)).toBeInTheDocument();
});

it("el nombre de pila solo no sugiere una ficha con apellido", async () => {
  abrir(fila({ cliente: "Ana" }), jest.fn());
  const cliente = await screen.findByLabelText("Cliente al que se le fió");
  await waitFor(() => expect(screen.getByRole("option", { name: "Ana Pérez" })).toBeInTheDocument());
  expect(cliente).toHaveValue("");
});

it("cambiar el medio a propósito viaja como marca explícita y sin cliente", async () => {
  const onSave = jest.fn();
  abrir(fila(), onSave);
  await elegirProducto();
  fireEvent.change(screen.getByLabelText("Forma de pago"), { target: { value: "cash" } });
  expect(screen.queryByLabelText("Cliente al que se le fió")).toBeNull();
  expect(screen.getByText(/entra como cobrada/)).toBeInTheDocument();
  importar();
  await waitFor(() =>
    expect(onSave).toHaveBeenCalledWith(
      "sale",
      expect.not.objectContaining({ customer_id: expect.anything() }),
      { cambiar_medio_de_pago: true },
    ),
  );
  expect(onSave.mock.calls[0][1]).toMatchObject({ payment_method: "cash" });
});
