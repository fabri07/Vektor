/**
 * B13 — el flujo de la caja contra el servicio mockeado.
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { AxiosError, type InternalAxiosRequestConfig } from "axios";

import { PosScreen } from "@/features/pos/PosScreen";
import type { PosOperationPayload, PosProduct } from "@/lib/pos/cart";
import { posService } from "@/services/pos.service";
import { useAuthStore } from "@/stores/authStore";
import { usePosTerminalStore } from "@/stores/posTerminalStore";

jest.mock("@/services/pos.service", () => ({
  posService: {
    lookup: jest.fn(),
    searchCatalog: jest.fn(),
    searchCustomers: jest.fn(),
    learnBarcode: jest.fn(),
    createOperation: jest.fn(),
    voidOperation: jest.fn(),
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
const azucar: PosProduct = { ...yerba, id: "p2", name: "Azúcar", sale_price_ars: "500.00" };

function resultado(total: string, vuelto: string | null = null) {
  return {
    id: "op1",
    client_operation_id: "x",
    total_ars: total,
    subtotal_ars: total,
    discount_ars: "0.00",
    cash_received_ars: null,
    cash_change_ars: vuelto,
    status: "COMPLETED",
  };
}

function httpError(status: number, data: unknown): AxiosError {
  const e = new AxiosError(`status ${status}`);
  e.response = { status, data, headers: {}, statusText: "", config: {} as InternalAxiosRequestConfig };
  return e;
}

function usuario(role: string, pos_permissions: string[] = []) {
  useAuthStore.setState({
    token: "t",
    user: { id: "u1", email: "a@b.com", full_name: "Ana", role, tenant_id: "t1", pos_permissions },
  });
}

/** Una lectura del lector: ráfaga sobre la pantalla + Enter. */
async function escanear(code: string) {
  await act(async () => {
    for (const ch of code) fireEvent.keyDown(document.body, { key: ch });
    fireEvent.keyDown(document.body, { key: "Enter" });
  });
}

const cobrarBtn = () => screen.getByRole("button", { name: /^Cobrar/ });

beforeEach(() => {
  jest.clearAllMocks();
  window.localStorage.clear();
  usuario("OWNER");
  usePosTerminalStore.setState({ terminal: null, invalid: false });
  svc.lookup.mockResolvedValue({ code_type: "gtin", matched_by: "barcode", product: yerba });
});

describe("flujo de cobro", () => {
  it("escanear → descuento → efectivo con vuelto → cobrada", async () => {
    svc.createOperation.mockResolvedValue(resultado("900.00", "100.00"));
    render(<PosScreen />);
    await escanear("7790001234567");
    expect(await screen.findByText("Yerba")).toBeInTheDocument();
    expect(screen.getByTestId("total")).toHaveTextContent("1.000,00");

    fireEvent.change(screen.getByLabelText("Descuento a la venta"), { target: { value: "100" } });
    expect(screen.getByTestId("total")).toHaveTextContent("900,00");
    fireEvent.change(screen.getByLabelText("Efectivo recibido"), { target: { value: "1000" } });
    expect(screen.getByTestId("vuelto-preview")).toHaveTextContent("100,00");

    await act(async () => {
      fireEvent.click(cobrarBtn());
    });
    expect(await screen.findByTestId("venta-cobrada")).toHaveTextContent("Vuelto");
    const payload = svc.createOperation.mock.calls[0]?.[0] as PosOperationPayload;
    expect(payload.discount_ars).toBe("100.00");
    expect(payload.cash_received_ars).toBe("1000.00");
    expect(payload.tenders).toEqual([{ payment_method: "cash", amount_ars: "900.00" }]);
    expect(payload.items).toEqual([{ product_id: "p1", quantity: 1, discount_ars: "0.00" }]);
    // Cobrada: ya no hay nada pendiente guardado.
    expect(window.localStorage.getItem("vektor-pos-pending-operation")).toBeNull();
  });

  it("sin respuesta: carrito bloqueado y el reintento reenvía el MISMO payload", async () => {
    svc.createOperation
      .mockRejectedValueOnce(new AxiosError("Network Error"))
      .mockResolvedValueOnce(resultado("1000.00"));
    render(<PosScreen />);
    await escanear("7790001234567");
    await screen.findByText("Yerba");
    await act(async () => {
      fireEvent.click(cobrarBtn());
    });
    expect(await screen.findByRole("button", { name: "Reintentar cobro" })).toBeInTheDocument();
    expect(screen.getByLabelText("Cantidad de Yerba")).toBeDisabled();
    // Un escaneo en duda no agrega nada.
    await escanear("7790001234567");
    expect(screen.getByText(/Terminá el cobro en curso/)).toBeInTheDocument();
    // Guardado para sobrevivir a una recarga.
    expect(window.localStorage.getItem("vektor-pos-pending-operation")).not.toBeNull();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Reintentar cobro" }));
    });
    await screen.findByTestId("venta-cobrada");
    const [primero, segundo] = svc.createOperation.mock.calls.map((c) => c[0]);
    expect(segundo).toEqual(primero);
    expect(window.localStorage.getItem("vektor-pos-pending-operation")).toBeNull();
  });

  it("un 409 IDEMPOTENCY_KEY_REUSED NO desbloquea el carrito: la venta existe", async () => {
    svc.createOperation.mockRejectedValue(
      httpError(409, { detail: { code: "IDEMPOTENCY_KEY_REUSED" } }),
    );
    render(<PosScreen />);
    await escanear("7790001234567");
    await screen.findByText("Yerba");
    await act(async () => {
      fireEvent.click(cobrarBtn());
    });
    expect(await screen.findByRole("button", { name: "Reintentar cobro" })).toBeInTheDocument();
    expect(screen.getByText(/No cobres de nuevo/)).toBeInTheDocument();
  });

  it("al abrir, un cobro que quedó en duda se reintenta antes que nada", async () => {
    const pendiente: PosOperationPayload = {
      client_operation_id: "op-viejo",
      customer_id: null,
      operation_date: "2026-09-25T10:00:00",
      discount_ars: "0.00",
      cash_received_ars: null,
      items: [{ product_id: "p1", quantity: 1, discount_ars: "0.00" }],
      tenders: [{ payment_method: "cash", amount_ars: "1000.00" }],
    };
    window.localStorage.setItem(
      "vektor-pos-pending-operation",
      JSON.stringify({
        tenantId: "t1",
        payload: pendiente,
        lines: [{ product: yerba, quantity: 1, discountCents: 0 }],
        savedAt: "x",
      }),
    );
    svc.createOperation.mockResolvedValue(resultado("1000.00"));
    render(<PosScreen />);
    await screen.findByTestId("venta-cobrada");
    expect(svc.createOperation).toHaveBeenCalledWith(pendiente);
    expect(window.localStorage.getItem("vektor-pos-pending-operation")).toBeNull();
  });

  it("un precio que cambió actualiza el carrito y deja volver a cobrar", async () => {
    svc.createOperation.mockRejectedValueOnce(
      httpError(422, {
        detail: {
          code: "TENDERS_MISMATCH",
          expected_total_ars: "1200.00",
          lines: [{ product_id: "p1", unit_price_list: "1200.00" }],
        },
      }),
    );
    render(<PosScreen />);
    await escanear("7790001234567");
    await screen.findByText("Yerba");
    await act(async () => {
      fireEvent.click(cobrarBtn());
    });
    expect(await screen.findByText(/Cambió el precio de Yerba/)).toBeInTheDocument();
    expect(screen.getByTestId("total")).toHaveTextContent("1.200,00");
    expect(cobrarBtn()).toBeEnabled();
    expect(window.localStorage.getItem("vektor-pos-pending-operation")).toBeNull();
  });

  it("sin stock: muestra el motivo y desbloquea", async () => {
    svc.createOperation.mockRejectedValueOnce(
      httpError(400, {
        detail: "No hay stock suficiente de Yerba.",
        code: "INSUFFICIENT_STOCK",
        product_id: "p1",
        available: 0,
        requested: 1,
      }),
    );
    render(<PosScreen />);
    await escanear("7790001234567");
    await screen.findByText("Yerba");
    await act(async () => {
      fireEvent.click(cobrarBtn());
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("No hay stock suficiente de Yerba.");
    expect(screen.getByText(/Stock disponible/)).toBeInTheDocument();
  });
});

describe("scan", () => {
  it("un código ambiguo pide elegir entre los candidatos", async () => {
    svc.lookup.mockRejectedValue(
      httpError(409, {
        detail: {
          code: "SCAN_AMBIGUOUS",
          candidates: [
            { product_id: "p1", name: "Yerba", matched_by: "barcode", product: yerba },
            { product_id: "p2", name: "Azúcar", matched_by: "sku", product: azucar },
          ],
        },
      }),
    );
    render(<PosScreen />);
    await escanear("7790001234567");
    expect(await screen.findByText("¿Cuál de estos productos es?")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Azúcar/ }));
    expect(await screen.findByLabelText("Cantidad de Azúcar")).toBeInTheDocument();
  });

  it("un código nuevo se aprende eligiendo el producto", async () => {
    svc.lookup.mockRejectedValue(
      httpError(404, { detail: { code: "SCAN_NOT_FOUND", learnable: true } }),
    );
    svc.searchCatalog.mockResolvedValue([azucar]);
    svc.learnBarcode.mockResolvedValue({ ...azucar, barcode: "7790001234567" });
    render(<PosScreen />);
    await escanear("7790001234567");
    fireEvent.change(await screen.findByLabelText("Buscar por nombre"), {
      target: { value: "azu" },
    });
    fireEvent.click(await screen.findByRole("button", { name: /Azúcar/ }));
    await waitFor(() => expect(svc.learnBarcode).toHaveBeenCalledWith("p2", "7790001234567"));
    expect(await screen.findByLabelText("Cantidad de Azúcar")).toBeInTheDocument();
  });

  it("sin permiso de aprender muestra el código crudo", async () => {
    usuario("CASHIER", []);
    usePosTerminalStore.setState({
      terminal: { terminalId: "c", name: "Caja 1", secret: "s", tenantId: "t1" },
      invalid: false,
    });
    svc.lookup.mockRejectedValue(
      httpError(404, { detail: { code: "SCAN_NOT_FOUND", learnable: true } }),
    );
    render(<PosScreen />);
    await escanear("VKT'0123456789AB");
    expect(await screen.findByText(/Código desconocido: «VKT'0123456789AB»/)).toBeInTheDocument();
  });
});

describe("acceso y permisos", () => {
  it("un cajero en una PC sin habilitar no ve la caja", () => {
    usuario("CASHIER", ["discount"]);
    render(<PosScreen />);
    expect(screen.getByText(/no está habilitada como caja/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Código de barras")).not.toBeInTheDocument();
  });

  it("una caja dada de baja tampoco", () => {
    usuario("CASHIER", []);
    usePosTerminalStore.setState({
      terminal: { terminalId: "c", name: "Caja 1", secret: "s", tenantId: "t1" },
      invalid: true,
    });
    render(<PosScreen />);
    expect(screen.getByText(/fue dada de baja/)).toBeInTheDocument();
  });

  it("un cajero sin permiso de descuento no ve el control", async () => {
    usuario("CASHIER", []);
    usePosTerminalStore.setState({
      terminal: { terminalId: "c", name: "Caja 1", secret: "s", tenantId: "t1" },
      invalid: false,
    });
    render(<PosScreen />);
    await escanear("7790001234567");
    await screen.findByText("Yerba");
    expect(screen.queryByLabelText("Descuento a la venta")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Descuento de Yerba")).not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /Fiado/ })).not.toBeInTheDocument();
  });

  it("anular el último ticket pide motivo y llama a la anulación", async () => {
    svc.createOperation.mockResolvedValue(resultado("1000.00"));
    svc.voidOperation.mockResolvedValue({ ...resultado("1000.00"), status: "VOIDED" });
    render(<PosScreen />);
    await escanear("7790001234567");
    await screen.findByText("Yerba");
    await act(async () => {
      fireEvent.click(cobrarBtn());
    });
    fireEvent.click(await screen.findByRole("button", { name: /Anular último ticket/ }));
    const confirmar = screen.getByRole("button", { name: "Confirmar anulación" });
    expect(confirmar).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Motivo de la anulación"), {
      target: { value: "cobré de más" },
    });
    await act(async () => {
      fireEvent.click(confirmar);
    });
    expect(svc.voidOperation).toHaveBeenCalledWith("op1", "cobré de más");
    expect(await screen.findByText(/anulado/)).toBeInTheDocument();
  });
});
