import React from "react";
import { fireEvent, render as rtlRender, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import type { UnclassifiedRecordResponse } from "@/services/others.service";
import { ReclassifyModal } from "../ReclassifyModal";

jest.mock("@/services/products.service", () => ({
  productsService: {
    getAllProducts: jest.fn().mockResolvedValue([
      { id: "product-1", name: "Agua mineral" },
      { id: "product-2", name: "Soda" },
    ]),
  },
}));

function render(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return rtlRender(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const record: UnclassifiedRecordResponse = {
  id: "record-1",
  uploaded_file_id: null,
  source: "ingestion",
  context_label: "Compra ambigua",
  headers: ["producto", "total", "cantidad"],
  row_data: { producto: "Agua", total: "600", cantidad: "4" },
  suggested_entity: "expense",
  suggested_category: "INVENTORY",
  suggested_category_label: "Mercadería",
  match_candidates: [
    {
      id: "product-1",
      matched_by: ["name"],
      name: "Agua mineral",
      sku: null,
      barcode: null,
    },
  ],
  status: "PENDING",
  created_at: "2026-07-18T10:00:00Z",
};

describe("resolución de compra ambigua (B·5b: con sus artículos)", () => {
  const abrir = (onResolvePurchase: jest.Mock) =>
    render(
      <ReclassifyModal
        record={record}
        productCategories={[]}
        saving={false}
        onClose={jest.fn()}
        onSave={jest.fn()}
        onLink={jest.fn()}
        onResolvePurchase={onResolvePurchase}
      />,
    );

  it("abre como compra con el candidato en la primera línea y manda las líneas", async () => {
    const onResolvePurchase = jest.fn();
    abrir(onResolvePurchase);

    // La fila llegó a /otros sin fecha (row_data no la trae) → hay que completarla.
    fireEvent.change(screen.getByLabelText("Fecha de la compra"), {
      target: { value: "2024-03-05" },
    });
    fireEvent.click(screen.getByRole("button", { name: /registrar compra/i }));

    await waitFor(() =>
      expect(onResolvePurchase).toHaveBeenCalledWith({
        lines: [{ product_id: "product-1", quantity: 4, amount: 600 }],
        // F6: la fecha va tal cual la completó el usuario, sin fallback a "hoy".
        transaction_date: "2024-03-05T00:00:00",
        payment_method: "cash",
      }),
    );
  });

  it("varias líneas con IVA incluido", async () => {
    const onResolvePurchase = jest.fn();
    abrir(onResolvePurchase);
    fireEvent.change(screen.getByLabelText("Fecha de la compra"), {
      target: { value: "2024-03-05" },
    });
    fireEvent.click(screen.getByRole("button", { name: /agregar artículo/i }));
    await screen.findAllByRole("option", { name: "Soda" });
    fireEvent.change(screen.getByLabelText("Artículo de la línea 2"), {
      target: { value: "product-2" },
    });
    fireEvent.change(screen.getByLabelText("Importe de la línea 2"), {
      target: { value: "300" },
    });
    fireEvent.change(screen.getByLabelText("IVA"), { target: { value: "incluido" } });
    fireEvent.change(screen.getByLabelText("Alícuota (%)"), { target: { value: "21" } });
    fireEvent.click(screen.getByRole("button", { name: /registrar compra/i }));

    await waitFor(() =>
      expect(onResolvePurchase).toHaveBeenCalledWith(
        expect.objectContaining({
          lines: [
            { product_id: "product-1", quantity: 4, amount: 600 },
            { product_id: "product-2", quantity: 1, amount: 300 },
          ],
          tax: { treatment: "incluido", rate: 0.21 },
        }),
      ),
    );
  });

  it("F6: NO resuelve la compra sin fecha (botón deshabilitado, sin fallback a hoy)", () => {
    const onResolvePurchase = jest.fn();
    abrir(onResolvePurchase);
    const btn = screen.getByRole("button", { name: /registrar compra/i });
    expect(btn).toBeDisabled();
    fireEvent.click(btn);
    expect(onResolvePurchase).not.toHaveBeenCalled();
  });

  it("mercadería no se ofrece como categoría de gasto", () => {
    abrir(jest.fn());
    fireEvent.change(screen.getByLabelText("Importar como"), { target: { value: "expense" } });
    expect(screen.queryByRole("option", { name: "Mercadería" })).not.toBeInTheDocument();
  });
});

describe("resolución de venta/gasto sin fecha (F6)", () => {
  const saleRecord = {
    ...record,
    id: "record-sale",
    context_label: "Fila sin fecha reconocible",
    headers: ["detalle", "monto"],
    row_data: { detalle: "Venta mostrador", monto: "5000" },
    suggested_entity: "sale",
    suggested_category: null,
    suggested_category_label: null,
    match_candidates: null,
  } as unknown as UnclassifiedRecordResponse;

  it("no importa una venta sin fecha (no inventa hoy)", () => {
    const onSave = jest.fn();
    render(
      <ReclassifyModal
        record={saleRecord}
        productCategories={[]}
        saving={false}
        onClose={jest.fn()}
        onSave={onSave}
        onLink={jest.fn()}
        onResolvePurchase={jest.fn()}
      />,
    );
    // Sin fecha el botón Importar está deshabilitado y el submit no dispara onSave.
    expect(screen.getByRole("button", { name: /importar/i })).toBeDisabled();
    fireEvent.submit(screen.getByRole("button", { name: /importar/i }).closest("form")!);
    expect(onSave).not.toHaveBeenCalled();
  });

  it("importa la venta con la fecha completada, sin fallback", async () => {
    const onSave = jest.fn();
    render(
      <ReclassifyModal
        record={saleRecord}
        productCategories={[]}
        saving={false}
        onClose={jest.fn()}
        onSave={onSave}
        onLink={jest.fn()}
        onResolvePurchase={jest.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText("Fecha"), { target: { value: "2024-03-05" } });
    // B·5e: una venta lleva producto.
    fireEvent.change(screen.getByPlaceholderText("Buscar producto"), { target: { value: "" } });
    await waitFor(() => expect(screen.getByRole("option", { name: "Soda" })).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Producto vendido"), { target: { value: "product-2" } });
    fireEvent.submit(screen.getByRole("button", { name: /importar/i }).closest("form")!);
    await waitFor(() =>
      expect(onSave).toHaveBeenCalledWith(
        "sale",
        expect.objectContaining({ transaction_date: "2024-03-05T00:00:00", product_id: "product-2" }),
      ),
    );
  });

  it("una fila sugerida como venta no ofrece «Compra de mercadería»", () => {
    render(
      <ReclassifyModal
        record={{ ...record, suggested_entity: "sale", match_candidates: null }}
        productCategories={[]}
        saving={false}
        onClose={jest.fn()}
        onSave={jest.fn()}
        onLink={jest.fn()}
        onResolvePurchase={jest.fn()}
      />,
    );
    expect(screen.queryByRole("option", { name: "Compra de mercadería" })).not.toBeInTheDocument();
  });
});
