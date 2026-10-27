/**
 * B13 — el flujo de la caja contra el servicio mockeado.
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { AxiosError, type InternalAxiosRequestConfig } from "axios";

import { PosScreen } from "@/features/pos/PosScreen";
import type { PosOperationPayload, PosProduct } from "@/lib/pos/cart";
import { _limpiarPosDb, guardarOperacion, listarOperaciones } from "@/lib/offline/posDb";
import { posService } from "@/services/pos.service";
import { useAuthStore } from "@/stores/authStore";
import { usePosPrintConfigStore } from "@/stores/posPrintConfigStore";
import { usePosCashSessionStore } from "@/stores/posCashSessionStore";
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
    listOperations: jest.fn().mockResolvedValue([]),
    openCashSession: jest.fn(),
    getCashSession: jest.fn(),
    listCashSessions: jest.fn().mockResolvedValue([]),
  },
}));
jest.mock("@/services/auth.service", () => ({ logoutRequest: jest.fn() }));
// La impresora del navegador: abre el diálogo nativo, en jsdom no existe.
const imprimir = jest.fn().mockResolvedValue(undefined);
jest.mock("@/lib/print/browserPrinter", () => ({
  createBrowserPrinter: () => ({ print: (...a: unknown[]) => imprimir(...a) }),
}));
// La base real, con `guardarOperacion` envuelto para poder hacerla fallar en el
// test que prueba que sin guardado NO se cobra. `jest.spyOn` sobre el namespace
// de un módulo no sirve: sus propiedades son de sólo lectura.
jest.mock("@/lib/offline/posDb", () => {
  const real = jest.requireActual("@/lib/offline/posDb");
  return {
    ...real,
    guardarOperacion: jest.fn((...args: unknown[]) =>
      (real as { guardarOperacion: (...a: unknown[]) => unknown }).guardarOperacion(...args),
    ),
  };
});

const svc = posService as jest.Mocked<typeof posService>;

/**
 * Un turno ya abierto (B8): sin turno la caja muestra la apertura y no cobra.
 * Se siembra para las dos cajas que usan estos tests (sin terminal y "c").
 */
function turnoAbierto() {
  const turno = {
    clientSessionId: "turno-test",
    serverId: "s1",
    openingFloatCents: 0,
    declaredFloatCents: 0,
    openedAt: "2026-09-29T09:00:00.000Z",
    aperturaConfirmada: true,
  };
  usePosCashSessionStore.setState({
    turnos: { "t1:u1:sin-terminal": turno, "t1:u1:c": turno },
  });
}


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

beforeEach(async () => {
  jest.clearAllMocks();
  window.localStorage.clear();
  // La cola de la caja vive en IndexedDB y sobrevive entre tests a propósito
  // (es su razón de ser): en los tests hay que vaciarla explícitamente.
  await _limpiarPosDb();
  usuario("OWNER");
  usePosTerminalStore.setState({ terminal: null, invalid: false });
  turnoAbierto();
  // La impresión se prueba aparte (pos_printing.test.tsx).
  usePosPrintConfigStore.setState({ autoPrint: false });
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

  // 402 = suscripción vencida; 423 = mantenimiento.
  it.each([
    [402, "SUBSCRIPTION_READ_ONLY"],
    [423, undefined],
  ])(
    "un %s en el PRIMER envío NO borra la venta: el cajero ya cobró",
    async (status, code) => {
      // El agujero más grave que tenía B7: al apretar Cobrar se registra
      // `charged_at` —la plata entró y el cliente se fue—, y un 4xx cualquiera
      // borraba la operación local. Con 402 y 423 no hay NADA que el cajero
      // pueda corregir: se arreglan cuando el dueño paga o termina el
      // mantenimiento. La venta no quedaba ni en el servidor ni en la cola.
      svc.createOperation.mockRejectedValue(
        httpError(status as number, code ? { detail: { code } } : { detail: "x" }),
      );
      svc.syncOperation.mockRejectedValue(
        httpError(status as number, code ? { detail: { code } } : { detail: "x" }),
      );
      render(<PosScreen />);
      await escanear("7790001234567");
      await screen.findByText("Yerba");
      await act(async () => {
        fireEvent.click(cobrarBtn());
      });
      await waitFor(async () => expect(await listarOperaciones("t1")).toHaveLength(1));
      const guardadas = await listarOperaciones("t1");
      expect(guardadas[0]!.estado).toBe("PENDIENTE");
      // Y no se le dice "sin conexión": el servidor contestó, y mandarlo a
      // revisar el router es mandarlo a arreglar lo que no está roto.
      expect(screen.queryByText(/cuando vuelva la conexión/)).not.toBeInTheDocument();
    },
  );

  it("un rechazo corregible NO borra el cobro: queda sin completar", async () => {
    // Al apretar Cobrar ya se afirmó `charged_at`, o sea que puede haber plata
    // recibida. Borrarlo —lo que se hacía— dejaba ese cobro sin ninguna
    // constancia si el cajero se iba antes de rehacerlo.
    svc.createOperation.mockRejectedValue(
      httpError(422, { detail: { code: "CASH_RECEIVED_WITHOUT_CASH_TENDER" } }),
    );
    render(<PosScreen />);
    await escanear("7790001234567");
    await screen.findByText("Yerba");
    await act(async () => {
      fireEvent.click(cobrarBtn());
    });
    // Se espera el ESTADO, no que exista: la operación se guarda como
    // `PENDIENTE` antes de mandar y recién después pasa a `EN_CORRECCION`, así
    // que esperar por la cantidad deja pasar el assert demasiado temprano.
    await waitFor(async () =>
      expect((await listarOperaciones("t1"))[0]?.estado).toBe("EN_CORRECCION"),
    );
    expect(await listarOperaciones("t1")).toHaveLength(1);
    // Y el carrito sigue editable para rehacer el cobro.
    await waitFor(() => expect(cobrarBtn()).toBeEnabled());
  });

  it("y desaparece cuando el cobro corregido entra", async () => {
    // El contrapeso: si quedara para siempre, cada cambio de precio dejaría
    // basura en la cola y el cajero no sabría cuál mirar.
    svc.createOperation
      .mockRejectedValueOnce(httpError(400, { detail: "Sin stock.", code: "INSUFFICIENT_STOCK", product_id: "p1", available: 0 }))
      .mockResolvedValueOnce(resultado("1000.00"));
    render(<PosScreen />);
    await escanear("7790001234567");
    await screen.findByText("Yerba");
    await act(async () => {
      fireEvent.click(cobrarBtn());
    });
    await waitFor(async () =>
      expect((await listarOperaciones("t1"))[0]?.estado).toBe("EN_CORRECCION"),
    );
    await waitFor(() => expect(cobrarBtn()).toBeEnabled());
    await act(async () => {
      fireEvent.click(cobrarBtn());
    });
    await screen.findByTestId("venta-cobrada");
    const guardadas = await listarOperaciones("t1");
    expect(guardadas.filter((o) => o.estado === "EN_CORRECCION")).toHaveLength(0);
  });

  it("sin respuesta: la venta queda en la cola y el carrito se LIBERA", async () => {
    // Hasta B7 la caja quedaba trabada "en duda" hasta que alguien resolviera
    // el cobro a mano. Ahora la venta ya está guardada, se sube sola, y el
    // cajero sigue atendiendo al cliente siguiente.
    svc.createOperation.mockRejectedValue(new AxiosError("Network Error"));
    svc.syncOperation.mockRejectedValue(new AxiosError("Network Error"));
    render(<PosScreen />);
    await escanear("7790001234567");
    await screen.findByText("Yerba");
    await act(async () => {
      fireEvent.click(cobrarBtn());
    });
    // Con la PC conectada no se afirma "sin conexión": se dice lo que se sabe.
    // Y el aviso es ámbar —cobrada, pero todavía no en el servidor—, no rojo.
    const aviso = await screen.findByText(/No se pudo contactar al servidor; se reintenta sola/);
    expect(aviso).toHaveClass("text-vk-warning");
    expect(screen.queryByText(/vuelva la conexión/)).not.toBeInTheDocument();
    // Se ve como un cobro: total y sello de pendiente, no un carrito en $0
    // (hasta 2026-10, sin red el cajero no sabía si había cobrado).
    const cierre = screen.getByTestId("venta-guardada");
    expect(cierre).toHaveTextContent("PENDIENTE DE SINCRONIZAR");
    expect(cierre).toHaveTextContent("$");
    // El comprobante sale del recibo local, sin ir al servidor.
    imprimir.mockClear();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Imprimir comprobante" }));
    });
    expect(imprimir).toHaveBeenCalledTimes(1);
    expect(svc.getReceipt).not.toHaveBeenCalled();
    // El carrito quedó vacío y se puede volver a escanear.
    expect(screen.queryByText("Yerba")).not.toBeInTheDocument();
    await escanear("7790001234567");
    expect(await screen.findByText("Yerba")).toBeInTheDocument();

    // La venta está guardada en la PC, con su payload congelado.
    const guardadas = await listarOperaciones("t1");
    expect(guardadas).toHaveLength(1);
    expect(guardadas[0]!.estado).toBe("PENDIENTE");
    expect(guardadas[0]!.payload).toEqual(svc.createOperation.mock.calls[0]?.[0]);
    expect(guardadas[0]!.ultimoTipo).toBe("transporte");
  });

  it("con el navegador desconectado sí promete subirla al volver la conexión", async () => {
    const onLine = jest.spyOn(window.navigator, "onLine", "get").mockReturnValue(false);
    try {
      svc.createOperation.mockRejectedValue(new AxiosError("Network Error"));
      svc.syncOperation.mockRejectedValue(new AxiosError("Network Error"));
      render(<PosScreen />);
      await escanear("7790001234567");
      await screen.findByText("Yerba");
      await act(async () => {
        fireEvent.click(cobrarBtn());
      });
      expect(
        await screen.findByText(/Se sube sola cuando vuelva la conexión/),
      ).toBeInTheDocument();
    } finally {
      onLine.mockRestore();
    }
  });

  it("si no se puede guardar en la PC, NO se cobra", async () => {
    // Guardar primero es la razón de ser de la cola: si falla y se manda igual,
    // un cierre del navegador con la respuesta en vuelo deja la venta en ningún
    // lado. Se avisa y no se envía.
    (guardarOperacion as jest.Mock).mockRejectedValueOnce(new Error("cuota llena"));
    render(<PosScreen />);
    await escanear("7790001234567");
    await screen.findByText("Yerba");
    await act(async () => {
      fireEvent.click(cobrarBtn());
    });
    expect(await screen.findByText(/NO se cobró/)).toBeInTheDocument();
    expect(svc.createOperation).not.toHaveBeenCalled();
  });

  it("un 409 IDEMPOTENCY_KEY_REUSED no borra la venta de la cola: existe", async () => {
    svc.createOperation.mockRejectedValue(
      httpError(409, { detail: { code: "IDEMPOTENCY_KEY_REUSED" } }),
    );
    svc.syncOperation.mockRejectedValue(
      httpError(409, { detail: { code: "IDEMPOTENCY_KEY_REUSED" } }),
    );
    render(<PosScreen />);
    await escanear("7790001234567");
    await screen.findByText("Yerba");
    await act(async () => {
      fireEvent.click(cobrarBtn());
    });
    await waitFor(async () => {
      const guardadas = await listarOperaciones("t1");
      expect(guardadas).toHaveLength(1);
    });
  });

  it("al abrir, la venta que B13 dejó en localStorage pasa a la cola y se sube", async () => {
    const pendiente: PosOperationPayload = {
      client_operation_id: "op-viejo",
      customer_id: null,
      operation_date: "2026-09-25T10:00:00",
      discount_ars: "0.00",
      cash_received_ars: null,
      charged_at: null,
      client_session_id: null,
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
    svc.syncOperation.mockResolvedValue(resultado("1000.00"));
    render(<PosScreen />);
    // Se RECLAMA con el contenido original y sin el header offline: su primer
    // envío ya ocurrió y la venta puede existir en el servidor. Cambiarle el
    // cuerpo acá movería la huella de idempotencia y el servidor contestaría
    // `IDEMPOTENCY_KEY_REUSED` sobre una venta que sí existe, en vez de
    // devolver su ticket.
    await waitFor(() =>
      expect(svc.syncOperation).toHaveBeenCalledWith(pendiente, { offline: false }),
    );
    // La clave vieja se borra: la migración no se repite.
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
    // A-4: viaja el turno abierto de la pantalla, de cuyo cajón sale el reintegro.
    expect(svc.voidOperation).toHaveBeenCalledWith("op1", "cobré de más", "turno-test");
    expect(await screen.findByText(/anulado/)).toBeInTheDocument();
  });
});

describe("pago mixto (B9)", () => {
  it("dividir → efectivo + débito → vuelto sobre el efectivo → dos pagos en el payload", async () => {
    svc.createOperation.mockResolvedValue(resultado("1000.00", "400.00"));
    render(<PosScreen />);
    await escanear("7790001234567");
    expect(await screen.findByText("Yerba")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Dividir pago" }));
    expect(screen.getByLabelText("Medio del pago 2")).toHaveValue("debit_card");
    fireEvent.change(screen.getByLabelText("Importe del pago 1"), { target: { value: "600" } });
    expect(screen.getByTestId("pago-sin-asignar")).toHaveTextContent("Falta asignar");
    fireEvent.change(screen.getByLabelText("Importe del pago 2"), { target: { value: "400" } });
    expect(screen.queryByTestId("pago-sin-asignar")).toBeNull();

    fireEvent.change(screen.getByLabelText("Efectivo recibido"), { target: { value: "1000" } });
    // Vuelto contra los $600 en efectivo, no contra el total.
    expect(screen.getByTestId("vuelto-preview")).toHaveTextContent("400,00");

    await act(async () => {
      fireEvent.click(cobrarBtn());
    });
    expect(await screen.findByTestId("venta-cobrada")).toBeInTheDocument();
    const payload = svc.createOperation.mock.calls[0]?.[0] as PosOperationPayload;
    expect(payload.tenders).toEqual([
      { payment_method: "cash", amount_ars: "600.00" },
      { payment_method: "debit_card", amount_ars: "400.00" },
    ]);
    expect(payload.cash_received_ars).toBe("1000.00");
  });

  it("si el servidor cambia el precio, el reparto queda y no se cobra solo", async () => {
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
    expect(await screen.findByText("Yerba")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Dividir pago" }));
    fireEvent.change(screen.getByLabelText("Importe del pago 1"), { target: { value: "600" } });
    fireEvent.change(screen.getByLabelText("Importe del pago 2"), { target: { value: "400" } });

    await act(async () => {
      fireEvent.click(cobrarBtn());
    });
    await waitFor(() => expect(screen.getByTestId("total")).toHaveTextContent("1.200,00"));
    expect(screen.getByLabelText("Importe del pago 1")).toHaveValue("600");
    expect(screen.getByLabelText("Importe del pago 2")).toHaveValue("400");
    expect(screen.getByTestId("pago-sin-asignar")).toHaveTextContent("Falta asignar");
    expect(svc.createOperation).toHaveBeenCalledTimes(1);
  });
});
