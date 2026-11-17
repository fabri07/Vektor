/**
 * «Pago a proveedor»: una fila que el import sacó de los gastos (no es un gasto: la compra
 * fiada ya contó). Se resuelve eligiendo A QUÉ proveedor se le pagó; sale de la caja y baja
 * su deuda. «Cuenta corriente» no es un medio con que se pague una deuda.
 */
import React from "react";
import { fireEvent, render as rtlRender, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import type { UnclassifiedRecordResponse } from "@/services/others.service";
import { ReclassifyModal } from "../ReclassifyModal";

jest.mock("@/services/products.service", () => ({
  productsService: { getAllProducts: jest.fn().mockResolvedValue([]) },
}));
jest.mock("@/services/customers.service", () => ({
  customersService: { getAllCustomers: jest.fn().mockResolvedValue([]) },
}));
jest.mock("@/services/suppliers.service", () => ({
  suppliersService: {
    getAllSuppliers: jest.fn().mockResolvedValue([
      { id: "s-textil", name: "Textil Sur", last_name: null },
      { id: "s-botones", name: "Botones SA", last_name: null },
    ]),
  },
}));

function render(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return rtlRender(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function fila(): UnclassifiedRecordResponse {
  return {
    id: "r-1",
    uploaded_file_id: null,
    source: "ingestion",
    context_label: "Pago a proveedor: indicá a cuál",
    headers: null as unknown as string[],
    row_data: {
      fecha: "2026-10-10",
      monto: "100000.00",
      medio_de_pago: "transfer",
      detalle: "Pago a proveedor",
    },
    suggested_entity: "pago_prov",
    suggested_category: null,
    suggested_category_label: null,
    match_candidates: null as unknown as [],
    status: "PENDING",
    created_at: "2026-10-11T10:00:00Z",
  } as UnclassifiedRecordResponse;
}

function abrir(onSave: jest.Mock) {
  render(
    <ReclassifyModal
      record={fila()}
      productCategories={[]}
      saving={false}
      onClose={jest.fn()}
      onSave={onSave}
      onLink={jest.fn()}
      onResolvePurchase={jest.fn()}
    />,
  );
}

it("abre como «Pago a proveedor» y pide el proveedor", async () => {
  abrir(jest.fn());
  expect(screen.getByLabelText("Importar como")).toHaveValue("pago_proveedor");
  expect(await screen.findByLabelText("Proveedor al que se le pagó")).toBeInTheDocument();
  expect(screen.queryByRole("option", { name: "Cuenta corriente" })).toBeNull();
});

it("sin proveedor no se manda y dice por qué", async () => {
  const onSave = jest.fn();
  abrir(onSave);
  await screen.findByLabelText("Proveedor al que se le pagó");
  fireEvent.click(screen.getByRole("button", { name: "Importar" }));
  expect(await screen.findByText("Elegí a qué proveedor se le pagó.")).toBeInTheDocument();
  expect(onSave).not.toHaveBeenCalled();
});

it("se manda como pago, con el proveedor, el monto y el medio", async () => {
  const onSave = jest.fn();
  abrir(onSave);
  const select = await screen.findByLabelText("Proveedor al que se le pagó");
  await waitFor(() => expect(screen.getByRole("option", { name: "Textil Sur" })).toBeInTheDocument());
  fireEvent.change(select, { target: { value: "s-textil" } });
  fireEvent.click(screen.getByRole("button", { name: "Importar" }));
  await waitFor(() =>
    expect(onSave).toHaveBeenCalledWith(
      "pago_proveedor",
      expect.objectContaining({
        supplier_id: "s-textil",
        amount: 100000,
        occurred_on: "2026-10-10",
        payment_method: "transfer",
      }),
    ),
  );
});
