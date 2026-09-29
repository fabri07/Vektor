/**
 * B8 — el turno de caja desde la pantalla: apertura obligatoria, turno que
 * sobrevive a la recarga, cierre ciego y el panel del encargado.
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { AxiosError, type InternalAxiosRequestConfig } from "axios";

import { PosScreen } from "@/features/pos/PosScreen";
import type { PosOperationPayload, PosProduct } from "@/lib/pos/cart";
import { _limpiarPosDb, actualizarOperacion, guardarOperacion } from "@/lib/offline/posDb";
import type { PosReceipt } from "@/features/pos/ticketTemplate";
import { type PosCashSession, posService } from "@/services/pos.service";
import { useAuthStore } from "@/stores/authStore";
import { usePosCashSessionStore } from "@/stores/posCashSessionStore";
import { usePosPrintConfigStore } from "@/stores/posPrintConfigStore";
import { usePosTerminalStore } from "@/stores/posTerminalStore";

jest.mock("@/services/pos.service", () => ({
  posService: {
    lookup: jest.fn(),
    searchCatalog: jest.fn(),
    searchCustomers: jest.fn(),
    learnBarcode: jest.fn(),
    createOperation: jest.fn(),
    syncOperation: jest.fn(),
    catalogPage: jest.fn(),
    voidOperation: jest.fn(),
    getReceipt: jest.fn(),
    listOperations: jest.fn(),
    openCashSession: jest.fn(),
    getCashSession: jest.fn(),
    listCashSessions: jest.fn(),
    closeCashSession: jest.fn(),
    abandonCashSession: jest.fn(),
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

function sesion(over: Partial<PosCashSession> = {}): PosCashSession {
  return {
    id: "s1",
    client_session_id: "turno-test",
    terminal_id: null,
    user_id: "u1",
    cashier_name: "Ana",
    opening_float_ars: "5000.00",
    float_declared: true,
    opened_at: "2026-09-29T09:00:00Z",
    closed_at: null,
    closed_by_user_id: null,
    expected_cash_ars: null,
    counted_cash_ars: null,
    difference_ars: null,
    notes: null,
    status: "OPEN",
    expected_cash_now_ars: null,
    late_sales: [],
    sales_count: null,
    ...over,
  };
}

function httpError(status: number, data: unknown): AxiosError {
  const e = new AxiosError(`status ${status}`);
  e.response = { status, data, headers: {}, statusText: "", config: {} as InternalAxiosRequestConfig };
  return e;
}
const sinRed = () => new AxiosError("Network Error");

const VIEJO = "2026-01-01T09:00:00Z";
const RECIEN = new Date().toISOString();

function usuario(id = "u1", role = "OWNER") {
  useAuthStore.setState({
    token: "t",
    user: { id, email: "a@b.com", full_name: "Ana", role, tenant_id: "t1", pos_permissions: [] },
  });
}

function turnoGuardado(over: Record<string, unknown> = {}) {
  usePosCashSessionStore.setState({
    turnos: {
      "t1:u1:sin-terminal": {
        clientSessionId: "turno-test",
        serverId: "s1",
        openingFloatCents: 500000,
        declaredFloatCents: 500000,
        openedAt: "2026-09-29T09:00:00.000Z",
        aperturaConfirmada: true,
        ...over,
      },
    },
  });
}

async function escanear(code: string) {
  await act(async () => {
    for (const ch of code) fireEvent.keyDown(document.body, { key: ch });
    fireEvent.keyDown(document.body, { key: "Enter" });
  });
}

function enLinea(valor: boolean) {
  Object.defineProperty(window.navigator, "onLine", { value: valor, configurable: true });
}

beforeEach(async () => {
  jest.clearAllMocks();
  window.localStorage.clear();
  await _limpiarPosDb();
  enLinea(true);
  usuario();
  usePosTerminalStore.setState({ terminal: null, invalid: false });
  usePosCashSessionStore.setState({ turnos: {} });
  usePosPrintConfigStore.setState({ autoPrint: false });
  svc.lookup.mockResolvedValue({ code_type: "gtin", matched_by: "barcode", product: yerba });
  svc.listOperations.mockResolvedValue([]);
  svc.listCashSessions.mockResolvedValue([]);
  svc.getCashSession.mockResolvedValue(sesion());
  svc.syncOperation.mockRejectedValue(sinRed());
  svc.catalogPage.mockRejectedValue(sinRed());
});

describe("apertura", () => {
  it("sin turno se muestra la apertura y no se puede escanear", async () => {
    render(<PosScreen />);
    expect(await screen.findByRole("heading", { name: "Abrir turno" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Código de barras")).not.toBeInTheDocument();
    await escanear("7790001234567");
    expect(svc.lookup).not.toHaveBeenCalled();
  });

  it("abre con el fondo declarado y el turno sobrevive a la recarga", async () => {
    svc.openCashSession.mockImplementation(async (cid: string) =>
      sesion({ client_session_id: cid }),
    );
    const { unmount } = render(<PosScreen />);
    fireEvent.change(await screen.findByLabelText("Fondo inicial"), { target: { value: "5000" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Abrir turno" }));
    });
    expect(svc.openCashSession).toHaveBeenCalledWith(expect.any(String), "5000.00");
    expect(await screen.findByTestId("turno-bar")).toHaveTextContent("5.000,00");
    const cid = svc.openCashSession.mock.calls[0]?.[0];

    // Recarga: el mismo turno, no otro.
    unmount();
    svc.getCashSession.mockResolvedValue(sesion({ client_session_id: cid }));
    render(<PosScreen />);
    expect(await screen.findByTestId("turno-bar")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Abrir turno" })).not.toBeInTheDocument();
    expect(Object.values(usePosCashSessionStore.getState().turnos)[0]?.clientSessionId).toBe(cid);
  });

  it("otro usuario en la misma PC no hereda el turno", async () => {
    turnoGuardado();
    usuario("u2");
    render(<PosScreen />);
    expect(await screen.findByRole("heading", { name: "Abrir turno" })).toBeInTheDocument();
  });

  it("sin red abre igual, cobra con el turno y la apertura se sincroniza después", async () => {
    svc.openCashSession.mockRejectedValueOnce(sinRed());
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
    render(<PosScreen />);
    fireEvent.change(await screen.findByLabelText("Fondo inicial"), { target: { value: "0" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Abrir turno" }));
    });
    expect(await screen.findByTestId("turno-bar")).toHaveTextContent("apertura sin sincronizar");

    await escanear("7790001234567");
    await screen.findByText("Yerba");
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^Cobrar/ }));
    });
    await screen.findByTestId("venta-cobrada");
    const cid = svc.openCashSession.mock.calls[0]?.[0];
    const payload = svc.createOperation.mock.calls[0]?.[0] as PosOperationPayload;
    expect(payload.client_session_id).toBe(cid);

    svc.openCashSession.mockResolvedValue(sesion({ client_session_id: cid, opening_float_ars: "0.00" }));
    await act(async () => {
      window.dispatchEvent(new Event("online"));
    });
    await waitFor(() =>
      expect(screen.getByTestId("turno-bar")).not.toHaveTextContent("apertura sin sincronizar"),
    );
  });

  it("si el servidor tiene otro fondo, la caja toma el real y lo avisa", async () => {
    svc.openCashSession.mockImplementation(async (cid: string) =>
      sesion({ client_session_id: cid, opening_float_ars: "8000.00" }),
    );
    render(<PosScreen />);
    fireEvent.change(await screen.findByLabelText("Fondo inicial"), { target: { value: "5000" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Abrir turno" }));
    });
    await waitFor(() =>
      expect(screen.getByTestId("turno-bar")).toHaveTextContent(/fondo \$\s8\.000,00/),
    );
    expect(screen.getByTestId("turno-bar")).toHaveTextContent("El fondo registrado es");
  });

  it("un turno que el encargado cerró se suelta y vuelve la apertura", async () => {
    turnoGuardado();
    svc.getCashSession.mockResolvedValue(sesion({ status: "ABANDONED", notes: "Limpieza" }));
    render(<PosScreen />);
    expect(await screen.findByRole("heading", { name: "Abrir turno" })).toBeInTheDocument();
    // No en silencio: el cajero sabe que su cajón se quedó sin arqueo.
    expect(screen.getByTestId("aviso-turno-cerrado")).toHaveTextContent(
      "El encargado cerró tu turno sin arqueo (Limpieza)",
    );
    expect(usePosCashSessionStore.getState().turnos).toEqual({});
  });

  it("avisa los turnos anteriores sin cerrar de este usuario en esta caja", async () => {
    svc.listCashSessions.mockResolvedValue([
      sesion({ id: "viejo1", client_session_id: "v1" }),
      sesion({ id: "viejo2", client_session_id: "v2" }),
      sesion({ id: "otro", client_session_id: "v3", user_id: "u9" }),
    ]);
    render(<PosScreen />);
    expect(await screen.findByText(/Tenés 2 turnos anteriores sin cerrar/)).toBeInTheDocument();
  });
});

describe("cierre ciego", () => {
  async function abrirCierre() {
    render(<PosScreen />);
    await act(async () => {
      fireEvent.click(await screen.findByRole("button", { name: "Cerrar turno" }));
    });
    return screen.findByLabelText("Efectivo contado");
  }

  it("no muestra el esperado antes de firmar, y después sí", async () => {
    turnoGuardado();
    svc.getCashSession.mockResolvedValue(
      sesion({ expected_cash_now_ars: "7000.00" }), // aunque el servidor lo mandara
    );
    svc.closeCashSession.mockResolvedValue(
      sesion({
        status: "CLOSED",
        expected_cash_ars: "7000.00",
        counted_cash_ars: "6900.00",
        difference_ars: "-100.00",
        closed_at: "2026-09-29T18:00:00Z",
      }),
    );
    const input = await abrirCierre();
    expect(screen.queryByText(/7\.000/)).not.toBeInTheDocument();
    fireEvent.change(input, { target: { value: "6900" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Firmar el arqueo" }));
    });
    expect(svc.closeCashSession).toHaveBeenCalledWith("s1", "6900.00", null);
    expect(await screen.findByTestId("arqueo-esperado")).toHaveTextContent("7.000,00");
    expect(screen.getByTestId("arqueo-diferencia")).toHaveTextContent("Faltan $ 100,00");

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Listo" }));
    });
    expect(await screen.findByRole("heading", { name: "Abrir turno" })).toBeInTheDocument();
    expect(usePosCashSessionStore.getState().turnos).toEqual({});
  });

  it("sin red no se puede firmar", async () => {
    turnoGuardado();
    const input = await abrirCierre();
    enLinea(false);
    await act(async () => {
      window.dispatchEvent(new Event("offline"));
    });
    fireEvent.change(input, { target: { value: "5000" } });
    expect(screen.getByRole("button", { name: "Firmar el arqueo" })).toBeDisabled();
    expect(screen.getByText(/Sin conexión: el arqueo lo calcula el servidor/)).toBeInTheDocument();
  });

  it("avisa lo pendiente del turno separando lo que está en corrección", async () => {
    turnoGuardado();
    const recibo = {} as PosReceipt;
    const base = {
      tenantId: "t1",
      userId: "u1",
      terminalId: null,
      lines: [],
      recibo,
    };
    const payload = (id: string, turno: string) =>
      ({
        client_operation_id: id,
        client_session_id: turno,
        items: [],
        tenders: [],
      }) as unknown as PosOperationPayload;
    await guardarOperacion({ ...base, clientOperationId: "a", payload: payload("a", "turno-test") });
    await guardarOperacion({ ...base, clientOperationId: "b", payload: payload("b", "turno-test") });
    await actualizarOperacion("b", { estado: "EN_CORRECCION" });
    // De OTRO turno: no entra en el aviso.
    await guardarOperacion({ ...base, clientOperationId: "c", payload: payload("c", "otro") });

    await abrirCierre();
    expect(await screen.findByTestId("cierre-sin-sincronizar")).toHaveTextContent("1 sin sincronizar");
    expect(screen.getByTestId("cierre-en-correccion")).toHaveTextContent("1 sin completar");
    expect(screen.getByRole("button", { name: "Cerrar igual y firmar" })).toBeInTheDocument();
  });

  it("si se perdió la respuesta, antes de reenviar pregunta y muestra lo firmado", async () => {
    turnoGuardado();
    svc.closeCashSession.mockRejectedValueOnce(sinRed());
    const input = await abrirCierre();
    fireEvent.change(input, { target: { value: "5000" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Firmar el arqueo" }));
    });
    expect(await screen.findByText(/No hubo respuesta del servidor/)).toBeInTheDocument();

    svc.getCashSession.mockResolvedValue(
      sesion({
        status: "CLOSED",
        expected_cash_ars: "5000.00",
        counted_cash_ars: "5000.00",
        difference_ars: "0.00",
      }),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Firmar el arqueo" }));
    });
    expect(await screen.findByTestId("arqueo-diferencia")).toHaveTextContent("Cierra justo");
    expect(svc.closeCashSession).toHaveBeenCalledTimes(1);
  });

  it("un 409 muestra lo que ya estaba firmado sin mandar otro conteo", async () => {
    turnoGuardado();
    svc.closeCashSession.mockRejectedValue(
      httpError(409, { detail: { code: "CASH_SESSION_ALREADY_CLOSED", message: "Ya se cerró." } }),
    );
    const input = await abrirCierre();
    svc.getCashSession.mockResolvedValue(sesion({ status: "ABANDONED", notes: "Basura." }));
    fireEvent.change(input, { target: { value: "5000" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Firmar el arqueo" }));
    });
    expect(await screen.findByTestId("arqueo-resultado")).toHaveTextContent("sin arqueo: Basura.");
    expect(svc.closeCashSession).toHaveBeenCalledTimes(1);
  });

  it("con el carrito armado no deja cerrar", async () => {
    turnoGuardado();
    render(<PosScreen />);
    await escanear("7790001234567");
    await screen.findByText("Yerba");
    fireEvent.click(screen.getByRole("button", { name: "Cerrar turno" }));
    expect(await screen.findByText(/antes de cerrar el turno/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Efectivo contado")).not.toBeInTheDocument();
  });
});

describe("venta mixta en el turno", () => {
  it("la venta lleva el turno y el arqueo espera el fondo más la parte en efectivo", async () => {
    turnoGuardado();
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
    render(<PosScreen />);
    await escanear("7790001234567");
    await screen.findByText("Yerba");
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Dividir pago" }));
    });
    const importes = screen.getAllByLabelText(/Importe/);
    fireEvent.change(importes[0]!, { target: { value: "400" } });
    fireEvent.change(importes[1]!, { target: { value: "600" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^Cobrar/ }));
    });
    await screen.findByTestId("venta-cobrada");
    const payload = svc.createOperation.mock.calls[0]?.[0] as PosOperationPayload;
    expect(payload.client_session_id).toBe("turno-test");
    const efectivo = payload.tenders
      .filter((t) => t.payment_method === "cash")
      .reduce((s, t) => s + Number(t.amount_ars), 0);
    expect(payload.tenders.length).toBe(2);

    // Al cerrar, el esperado que devuelve el servidor es fondo + parte en
    // efectivo, y es lo que se muestra.
    svc.closeCashSession.mockResolvedValue(
      sesion({
        status: "CLOSED",
        expected_cash_ars: (5000 + efectivo).toFixed(2),
        counted_cash_ars: (5000 + efectivo).toFixed(2),
        difference_ars: "0.00",
      }),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Nueva venta" }));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Cerrar turno" }));
    });
    fireEvent.change(await screen.findByLabelText("Efectivo contado"), {
      target: { value: (5000 + efectivo).toFixed(2) },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Firmar el arqueo" }));
    });
    expect(await screen.findByTestId("arqueo-esperado")).toHaveTextContent(
      efectivo === 400 ? "5.400,00" : "5.600,00",
    );
  });
});

describe("panel del encargado", () => {
  it("cierra sin arqueo los elegidos, uno por request, con motivo obligatorio", async () => {
    turnoGuardado();
    svc.listCashSessions.mockResolvedValue([
      sesion({ id: "actual", client_session_id: "turno-test", sales_count: 3 }),
      // Basura: sin fondo declarado y viejos. Con ventas, como los de producción.
      sesion({ id: "b1", client_session_id: "v1", sales_count: 4, float_declared: false, opened_at: VIEJO }),
      sesion({ id: "b2", client_session_id: "v2", sales_count: 1, float_declared: false, opened_at: VIEJO }),
      // En uso en otra caja: recién abierto, con fondo declarado y sin ventas todavía.
      sesion({ id: "vivo", client_session_id: "v3", sales_count: 0, float_declared: true, opened_at: RECIEN }),
      // Recién abierto sin red: todavía sin fondo declarado, pero NO es viejo.
      sesion({ id: "sin-red", client_session_id: "v4", sales_count: 0, float_declared: false, opened_at: RECIEN }),
    ]);
    svc.abandonCashSession.mockResolvedValue(sesion({ status: "ABANDONED" }));
    render(<PosScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "Turnos" }));
    await screen.findByTestId("turnos-panel");
    await act(async () => {
      fireEvent.click(
        await screen.findByRole("button", { name: "Elegir los viejos sin fondo declarado (2)" }),
      );
    });
    const boton = screen.getByRole("button", { name: "Cerrar sin arqueo (2)" });
    expect(boton).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Motivo del cierre sin arqueo"), {
      target: { value: "Turnos de recargas de la pantalla" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Cerrar sin arqueo (2)" }));
    });
    expect(svc.abandonCashSession).toHaveBeenCalledTimes(2);
    expect(svc.abandonCashSession).toHaveBeenCalledWith("b1", "Turnos de recargas de la pantalla");
    expect(svc.abandonCashSession).toHaveBeenCalledWith("b2", "Turnos de recargas de la pantalla");
    expect(await screen.findByText(/Se cerraron 2 turnos sin arqueo/)).toBeInTheDocument();
  });

  it("el turno de esta pantalla no se puede elegir", async () => {
    turnoGuardado();
    svc.listCashSessions.mockResolvedValue([
      sesion({ id: "actual", client_session_id: "turno-test", sales_count: 0 }),
    ]);
    render(<PosScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "Turnos" }));
    const casilla = await screen.findByLabelText(/Elegir el turno/);
    expect(casilla).toBeDisabled();
  });

  it("un cajero no ve el panel", async () => {
    usuario("u1", "CASHIER");
    usePosTerminalStore.setState({
      terminal: { terminalId: "c", name: "Caja 1", secret: "s", tenantId: "t1" },
      invalid: false,
    });
    usePosCashSessionStore.setState({
      turnos: {
        "t1:u1:c": {
          clientSessionId: "turno-test",
          serverId: "s1",
          openingFloatCents: 0,
          declaredFloatCents: 0,
          openedAt: "2026-09-29T09:00:00.000Z",
          aperturaConfirmada: true,
        },
      },
    });
    render(<PosScreen />);
    expect(await screen.findByTestId("turno-bar")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Turnos" })).not.toBeInTheDocument();
  });
});


describe("revisión del turno durante la jornada", () => {
  it("si el encargado cierra el turno desde otra PC, la caja se entera sola", async () => {
    jest.useFakeTimers({ doNotFake: ["queueMicrotask", "nextTick", "setImmediate"] });
    try {
      turnoGuardado();
      render(<PosScreen />);
      expect(await screen.findByTestId("turno-bar")).toBeInTheDocument();
      svc.getCashSession.mockResolvedValue(sesion({ status: "ABANDONED" }));
      await act(async () => {
        jest.advanceTimersByTime(61_000);
      });
      expect(await screen.findByTestId("aviso-turno-cerrado")).toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });

  it("con el cierre abierto, el lector no agrega productos a un carrito oculto", async () => {
    turnoGuardado();
    render(<PosScreen />);
    await act(async () => {
      fireEvent.click(await screen.findByRole("button", { name: "Cerrar turno" }));
    });
    await screen.findByLabelText("Efectivo contado");
    await escanear("7790001234567");
    expect(svc.lookup).not.toHaveBeenCalled();
  });

  it("después de un cobro se puede cerrar sin apretar Nueva venta", async () => {
    turnoGuardado();
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
    render(<PosScreen />);
    await escanear("7790001234567");
    await screen.findByText("Yerba");
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^Cobrar/ }));
    });
    await screen.findByTestId("venta-cobrada");
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Cerrar turno" }));
    });
    expect(await screen.findByLabelText("Efectivo contado")).toBeInTheDocument();
  });

  it("si la apertura ya está en vuelo, el cierre la comparte en vez de fallar", async () => {
    turnoGuardado({ serverId: null, aperturaConfirmada: false });
    let soltarApertura: (s: PosCashSession) => void = () => {};
    svc.openCashSession.mockImplementation(
      () => new Promise<PosCashSession>((r) => (soltarApertura = r)),
    );
    svc.closeCashSession.mockResolvedValue(
      sesion({ status: "CLOSED", expected_cash_ars: "5000.00", counted_cash_ars: "5000.00", difference_ars: "0.00" }),
    );
    render(<PosScreen />);
    await screen.findByTestId("turno-bar");
    // El reintento automático manda la apertura y queda en vuelo...
    await act(async () => {
      window.dispatchEvent(new Event("online"));
    });
    expect(svc.openCashSession).toHaveBeenCalledTimes(1);
    // ...y el cajero abre el cierre justo ahí: comparte ese envío, no manda otro
    // ni falla con "no se pudo avisar la apertura".
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Cerrar turno" }));
    });
    await act(async () => {
      soltarApertura(sesion());
    });
    fireEvent.change(await screen.findByLabelText("Efectivo contado"), { target: { value: "5000" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Firmar el arqueo" }));
    });
    expect(await screen.findByTestId("arqueo-diferencia")).toHaveTextContent("Cierra justo");
    expect(svc.openCashSession).toHaveBeenCalledTimes(1);
    expect(svc.closeCashSession).toHaveBeenCalledWith("s1", "5000.00", null);
  });
});
