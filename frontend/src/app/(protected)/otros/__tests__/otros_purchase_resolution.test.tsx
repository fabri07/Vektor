import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import type { UnclassifiedRecordResponse } from "@/services/others.service";
import { ReclassifyModal } from "../ReclassifyModal";

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

describe("resolución de compra ambigua", () => {
  it("envía cantidad/costo/fecha al resolver (con fecha completada)", async () => {
    const onResolvePurchase = jest.fn();
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

    // La fila llegó a /otros sin fecha (row_data no la trae) → hay que completarla.
    fireEvent.change(screen.getByLabelText("Fecha"), { target: { value: "2024-03-05" } });
    fireEvent.change(screen.getByLabelText("Costo unitario (opcional)"), {
      target: { value: "140" },
    });
    fireEvent.click(screen.getByRole("button", { name: /registrar compra/i }));

    await waitFor(() =>
      expect(onResolvePurchase).toHaveBeenCalledWith(
        "product-1",
        expect.objectContaining({
          amount: 600,
          quantity: 4,
          unitCost: 140,
          // F6: la fecha va tal cual la completó el usuario, sin fallback a "hoy".
          transactionDate: "2024-03-05T00:00:00",
        }),
      ),
    );
  });

  it("F6: NO resuelve la compra sin fecha (botón deshabilitado, sin fallback a hoy)", () => {
    const onResolvePurchase = jest.fn();
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

    const btn = screen.getByRole("button", { name: /registrar compra/i });
    expect(btn).toBeDisabled();
    fireEvent.click(btn);
    expect(onResolvePurchase).not.toHaveBeenCalled();
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
    fireEvent.submit(screen.getByRole("button", { name: /importar/i }).closest("form")!);
    await waitFor(() =>
      expect(onSave).toHaveBeenCalledWith(
        "sale",
        expect.objectContaining({ transaction_date: "2024-03-05T00:00:00" }),
      ),
    );
  });
});
