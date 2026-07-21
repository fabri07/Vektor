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
  it("muestra candidatos y envía cantidad/costo al resolver", async () => {
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

    expect(screen.getByRole("button", { name: /registrar compra/i })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Costo unitario (opcional)"), {
      target: { value: "140" },
    });
    fireEvent.click(screen.getByRole("button", { name: /registrar compra/i }));

    await waitFor(() =>
      expect(onResolvePurchase).toHaveBeenCalledWith(
        "product-1",
        expect.objectContaining({ amount: 600, quantity: 4, unitCost: 140 }),
      ),
    );
  });
});
