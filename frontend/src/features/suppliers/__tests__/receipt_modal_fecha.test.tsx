/**
 * B·5b / F6: el remito pide la fecha (nunca «hoy» en silencio) y manda el IVA con
 * la misma regla que «Registrar compra».
 */
import "@testing-library/jest-dom";
import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ReceiptModal } from "../ReceiptModal";
import { suppliersService, type SupplierResponse } from "@/services/suppliers.service";

jest.mock("@/services/suppliers.service", () => ({
  suppliersService: { uploadReceipt: jest.fn(), extractReceipt: jest.fn() },
}));

jest.mock("@/stores/toastStore", () => ({
  useToastStore: (selector: (s: { add: jest.Mock }) => unknown) =>
    selector({ add: jest.fn() }),
}));

const upload = suppliersService.uploadReceipt as jest.Mock;

function pintar() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <ReceiptModal
        supplier={{ id: "s1", name: "Distribuidora Sur" } as SupplierResponse}
        isOpen
        onClose={jest.fn()}
        onUploaded={jest.fn()}
      />
    </QueryClientProvider>,
  );
  fireEvent.change(screen.getByPlaceholderText("Ej: Yerba 1kg"), { target: { value: "Yerba" } });
  const [qty, precio] = screen.getAllByPlaceholderText("0");
  fireEvent.change(qty!, { target: { value: "10" } });
  fireEvent.change(precio!, { target: { value: "800" } });
}

beforeEach(() => {
  jest.clearAllMocks();
  upload.mockResolvedValue({});
});

test("sin fecha no manda el remito", () => {
  pintar();
  expect(screen.getByLabelText("Fecha del remito")).toBeRequired();
  fireEvent.click(screen.getByRole("button", { name: "Guardar remito" }));
  expect(upload).not.toHaveBeenCalled();
});

test("con fecha e IVA incluido, los manda", async () => {
  pintar();
  fireEvent.change(screen.getByLabelText("Fecha del remito"), {
    target: { value: "2026-09-15" },
  });
  fireEvent.change(screen.getByLabelText("IVA"), { target: { value: "incluido" } });
  fireEvent.change(screen.getByLabelText("Alícuota (%)"), { target: { value: "21" } });
  fireEvent.click(screen.getByRole("button", { name: "Guardar remito" }));
  await waitFor(() =>
    expect(upload).toHaveBeenCalledWith(
      "s1",
      expect.objectContaining({
        transaction_date: "2026-09-15T00:00:00",
        tax: { treatment: "incluido", rate: 0.21 },
        currency: "ARS",
      }),
      expect.any(String),
    ),
  );
});
