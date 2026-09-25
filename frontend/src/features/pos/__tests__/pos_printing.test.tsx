/**
 * B12 — imprimir desde la caja: siempre después del cobro, y un fallo de
 * impresión nunca pone en duda la venta.
 */
import { act, fireEvent, render, screen } from "@testing-library/react";

import { PosScreen } from "@/features/pos/PosScreen";
import type { PosReceipt } from "@/features/pos/ticketTemplate";
import type { PosProduct } from "@/lib/pos/cart";
import { posService } from "@/services/pos.service";
import { useAuthStore } from "@/stores/authStore";
import { usePosPrintConfigStore } from "@/stores/posPrintConfigStore";
import { usePosTerminalStore } from "@/stores/posTerminalStore";

const mockPrint = jest.fn();
jest.mock("@/lib/print/browserPrinter", () => ({
  createBrowserPrinter: () => ({ print: (...a: unknown[]) => mockPrint(...a) }),
}));
jest.mock("@/services/pos.service", () => ({
  posService: {
    lookup: jest.fn(),
    createOperation: jest.fn(),
    getReceipt: jest.fn(),
    listOperations: jest.fn().mockResolvedValue([]),
    voidOperation: jest.fn(),
    searchCatalog: jest.fn(),
    searchCustomers: jest.fn(),
    learnBarcode: jest.fn(),
  },
}));
jest.mock("@/services/auth.service", () => ({ logoutRequest: jest.fn() }));

const svc = posService as jest.Mocked<typeof posService>;

const yerba: PosProduct = {
  id: "p1",
  name: "Yerba",
  sale_price_ars: "1000.00",
  sale_unit: "unit",
  base_units_per_sale_unit: 1,
  stock_units: 10,
};

const recibo: PosReceipt = {
  id: "op1",
  number: "OP1",
  business_name: "Kiosco",
  operation_date: "2026-09-25T10:00:00",
  status: "COMPLETED",
  lines: [
    {
      position: 0,
      product_name: "Yerba",
      quantity: 1,
      quantity_display: "1 u.",
      unit_price_list: "1000.00",
      gross_ars: "1000.00",
      discount_line_ars: "0.00",
      line_total_ars: "1000.00",
    },
  ],
  tenders: [{ payment_method: "cash", amount_ars: "1000.00" }],
  subtotal_ars: "1000.00",
  discount_ars: "0.00",
  total_ars: "1000.00",
  cash_received_ars: null,
  cash_change_ars: null,
  customer_name: null,
  cashier_name: "Ana",
  terminal_name: null,
};

async function cobrarUnaYerba() {
  render(<PosScreen />);
  await act(async () => {
    for (const ch of "7790001234567") fireEvent.keyDown(document.body, { key: ch });
    fireEvent.keyDown(document.body, { key: "Enter" });
  });
  await screen.findByText("Yerba");
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: /^Cobrar/ }));
  });
  await screen.findByTestId("venta-cobrada");
}

beforeEach(() => {
  jest.clearAllMocks();
  window.localStorage.clear();
  useAuthStore.setState({
    token: "t",
    user: { id: "u1", email: "a@b.com", full_name: "Ana", role: "OWNER", tenant_id: "t1" },
  });
  usePosTerminalStore.setState({ terminal: null, invalid: false });
  usePosPrintConfigStore.setState({ autoPrint: true, paperWidth: 80, pageSize: "A" });
  svc.lookup.mockResolvedValue({ code_type: "gtin", matched_by: "barcode", product: yerba });
  svc.createOperation.mockResolvedValue({
    id: "op1",
    client_operation_id: "x",
    total_ars: "1000.00",
    subtotal_ars: "1000.00",
    discount_ars: "0.00",
    cash_received_ars: null,
    cash_change_ars: null,
    status: "COMPLETED",
  });
  svc.getReceipt.mockResolvedValue(recibo);
  mockPrint.mockResolvedValue(undefined);
});

describe("impresión desde la caja", () => {
  it("al cobrar imprime el recibo DEL SERVIDOR, después de confirmar", async () => {
    await cobrarUnaYerba();
    await act(async () => {});
    expect(svc.getReceipt).toHaveBeenCalledWith("op1");
    // Primero la venta, después el recibo.
    expect(svc.createOperation.mock.invocationCallOrder[0]).toBeLessThan(
      svc.getReceipt.mock.invocationCallOrder[0]!,
    );
    expect(mockPrint).toHaveBeenCalledTimes(1);
    expect(mockPrint.mock.calls[0]?.[0].bodyHtml).toContain("Comprobante no fiscal");
  });

  it("si la impresión falla, la venta sigue cobrada y se puede reimprimir", async () => {
    mockPrint.mockRejectedValueOnce(new Error("sin papel"));
    await cobrarUnaYerba();
    expect(await screen.findByText(/No se pudo imprimir el ticket/)).toBeInTheDocument();
    expect(screen.getByTestId("venta-cobrada")).toBeInTheDocument();
    expect(svc.createOperation).toHaveBeenCalledTimes(1);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Reimprimir ticket" }));
    });
    expect(mockPrint).toHaveBeenCalledTimes(2);
    expect(svc.createOperation).toHaveBeenCalledTimes(1); // reimprimir no cobra
  });

  it("con 'Imprimir al cobrar' apagado no imprime solo", async () => {
    usePosPrintConfigStore.setState({ autoPrint: false });
    await cobrarUnaYerba();
    await act(async () => {});
    expect(svc.getReceipt).not.toHaveBeenCalled();
    expect(mockPrint).not.toHaveBeenCalled();
  });

  it("'Últimos tickets' reimprime uno viejo", async () => {
    svc.listOperations.mockResolvedValue([
      {
        id: "viejo",
        number: "VIEJO",
        operation_date: "2026-09-25T09:00:00",
        total_ars: "500.00",
        status: "COMPLETED",
        cashier_name: "Ana",
      },
    ]);
    usePosPrintConfigStore.setState({ autoPrint: false });
    render(<PosScreen />);
    fireEvent.click(screen.getByRole("button", { name: "Últimos tickets" }));
    const reimprimir = await screen.findByRole("button", { name: "Reimprimir" });
    await act(async () => {
      fireEvent.click(reimprimir);
    });
    expect(svc.getReceipt).toHaveBeenCalledWith("viejo");
    expect(mockPrint).toHaveBeenCalledTimes(1);
  });
});
