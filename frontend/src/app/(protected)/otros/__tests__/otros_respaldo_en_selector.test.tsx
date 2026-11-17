/**
 * B·5e en el selector de «Otros»: el producto se elige sabiendo si tiene compra o stock
 * inicial a la fecha de la venta. Los respaldados van primero, el resto queda marcado (no
 * bloqueado: decide el servidor), y si el stock figura desde DESPUÉS se puede confirmar
 * desde cuándo se tenía ahí mismo.
 */
import React from "react";
import { fireEvent, render as rtlRender, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import type { UnclassifiedRecordResponse } from "@/services/others.service";
import { productsService } from "@/services/products.service";
import { ReclassifyModal } from "../ReclassifyModal";

jest.mock("@/services/products.service", () => ({
  productsService: {
    getAllProducts: jest.fn().mockResolvedValue([
      { id: "p-nuevo", name: "Jean azul T40", primer_dia_de_fuente: "2026-10-25" },
      { id: "p-viejo", name: "Jeans azules T42", primer_dia_de_fuente: "2026-10-03" },
      { id: "p-nada", name: "Buzo gris", primer_dia_de_fuente: null },
    ]),
  },
}));
jest.mock("@/services/customers.service", () => ({
  customersService: { getAllCustomers: jest.fn().mockResolvedValue([]) },
}));

function fila(): UnclassifiedRecordResponse {
  return {
    id: "r-1",
    uploaded_file_id: null,
    source: "ingestion",
    context_label: "Venta sin producto",
    headers: null as unknown as string[],
    row_data: { fecha: "2026-10-22", monto: "20000.00", cantidad: "1", producto: "Jean" },
    suggested_entity: "sale",
    suggested_category: null,
    suggested_category_label: null,
    match_candidates: null as unknown as [],
    status: "PENDING",
    created_at: "2026-10-23T10:00:00Z",
  } as UnclassifiedRecordResponse;
}

function abrir() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  rtlRender(
    <QueryClientProvider client={qc}>
      <ReclassifyModal
        record={fila()}
        productCategories={[]}
        saving={false}
        onClose={jest.fn()}
        onSave={jest.fn()}
        onLink={jest.fn()}
        onResolvePurchase={jest.fn()}
      />
    </QueryClientProvider>,
  );
}

it("pide el catálogo con la fuente de cada producto", async () => {
  abrir();
  await screen.findByLabelText("Producto vendido");
  expect(productsService.getAllProducts).toHaveBeenCalledWith(
    expect.objectContaining({ con_fuente: true }),
  );
});

it("ordena primero los respaldados a la fecha de la venta y marca el resto", async () => {
  abrir();
  const select = await screen.findByLabelText("Producto vendido");
  await waitFor(() =>
    expect(within(select).getAllByRole("option").length).toBeGreaterThanOrEqual(3),
  );
  const nombres = within(select)
    .getAllByRole("option")
    .map((o) => o.textContent);
  expect(nombres).toEqual([
    "Elegí el producto",
    "Jeans azules T42",
    "Jean azul T40 · sin compra ni stock antes del 25/10/2026",
  ]); // la búsqueda arranca con lo que decía el archivo («Jean»)
});

it("un producto sin ninguna fuente dice que no tiene compra ni stock inicial", async () => {
  abrir();
  const busqueda = await screen.findByPlaceholderText("Buscar producto");
  fireEvent.change(busqueda, { target: { value: "buzo" } });
  expect(
    await screen.findByRole("option", { name: "Buzo gris · sin compra ni stock inicial" }),
  ).toBeInTheDocument();
});

it("si el stock figura desde después de la venta, se puede confirmar desde cuándo ahí mismo", async () => {
  abrir();
  const select = await screen.findByLabelText("Producto vendido");
  await screen.findByRole("option", { name: /Jean azul T40/ });
  fireEvent.change(select, { target: { value: "p-nuevo" } });
  expect(await screen.findByText(/El stock figura desde 25\/10\/2026/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Confirmar fecha" })).toBeInTheDocument();
  fireEvent.change(select, { target: { value: "p-viejo" } });
  await waitFor(() => expect(screen.queryByText(/El stock figura desde/)).toBeNull());
});
