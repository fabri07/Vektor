/**
 * B·5e: una venta mandada a «Otros» («Venta sin producto» / «sin compra previa»)
 * se resuelve eligiendo el producto. Antes el modal no lo dejaba elegir y la
 * venta nunca podía salir de «Otros».
 */
import React from "react";
import { fireEvent, render as rtlRender, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import type { UnclassifiedRecordResponse } from "@/services/others.service";
import { ReclassifyModal } from "../ReclassifyModal";

jest.mock("@/services/products.service", () => ({
  productsService: {
    getAllProducts: jest.fn().mockResolvedValue([
      { id: "p-vela", name: "Vela aromática" },
      { id: "p-yerba", name: "Yerba" },
    ]),
  },
}));

function render(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return rtlRender(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const record: UnclassifiedRecordResponse = {
  id: "r-1",
  uploaded_file_id: null,
  source: "ingestion",
  context_label: "Venta sin producto: elegí qué producto se vendió",
  headers: null as unknown as string[],
  row_data: { fecha: "2026-10-10", monto: "2000", cantidad: "2", producto: "Vela" },
  suggested_entity: "sale",
  suggested_category: null,
  suggested_category_label: null,
  match_candidates: null as unknown as [],
  status: "PENDING",
  created_at: "2026-10-11T10:00:00Z",
} as UnclassifiedRecordResponse;

function abrir(onSave: jest.Mock) {
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

it("arranca buscando lo que decía el archivo y manda el producto elegido", async () => {
  const onSave = jest.fn();
  abrir(onSave);
  expect(screen.getByPlaceholderText("Buscar producto")).toHaveValue("Vela");
  const select = await screen.findByLabelText("Producto vendido");
  await waitFor(() => expect(screen.getByRole("option", { name: "Vela aromática" })).toBeInTheDocument());
  // El filtro por lo que decía el archivo deja afuera lo que no coincide.
  expect(screen.queryByRole("option", { name: "Yerba" })).toBeNull();
  fireEvent.change(select, { target: { value: "p-vela" } });
  fireEvent.click(screen.getByRole("button", { name: /importar|guardar|clasificar/i }));
  await waitFor(() =>
    expect(onSave).toHaveBeenCalledWith(
      "sale",
      expect.objectContaining({ product_id: "p-vela", quantity: 2, amount: 2000 }),
    ),
  );
});

it("sin producto no se manda y dice por qué", async () => {
  const onSave = jest.fn();
  abrir(onSave);
  await screen.findByLabelText("Producto vendido");
  fireEvent.click(screen.getByRole("button", { name: /importar|guardar|clasificar/i }));
  expect(await screen.findByText(/Elegí qué producto se vendió/)).toBeInTheDocument();
  expect(onSave).not.toHaveBeenCalled();
});
