/**
 * Una PC rehabilitada como caja recibe una terminal con OTRO id, y las ventas
 * que cobró antes conservan el viejo: la cola sólo sube lo de la caja actual.
 * Hasta este arreglo quedaban trabadas para siempre y "Reintentar" no las
 * destrababa. Lo que se protege: que no suban solas (cambiar de caja lo
 * autoriza el dueño, con PIN, y queda auditado), que después de autorizarlo
 * suban UNA vez, y que si el servidor no lo autoriza no se reescriba nada.
 */
import { act, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { AxiosError, type InternalAxiosRequestConfig } from "axios";

import { PendingOperationsPanel } from "@/features/pos/PendingOperationsPanel";
import { usePosSync } from "@/features/pos/usePosSync";
import type { PosReceipt } from "@/features/pos/ticketTemplate";
import {
  _limpiarPosDb,
  deOtraCaja,
  guardarOperacion,
  listarOperaciones,
  reasignarTerminal,
} from "@/lib/offline/posDb";
import type { PosOperationPayload } from "@/lib/pos/cart";
import { posService } from "@/services/pos.service";

jest.mock("@/services/pos.service", () => ({
  posService: { syncOperation: jest.fn(), adoptPendingOperations: jest.fn() },
}));
const svc = posService as jest.Mocked<typeof posService>;

const recibo = { status: "PENDIENTE" } as unknown as PosReceipt;

function payload(id: string): PosOperationPayload {
  return {
    client_operation_id: id,
    customer_id: null,
    operation_date: "2026-10-02T10:00:00",
    discount_ars: "0.00",
    cash_received_ars: null,
    charged_at: "2026-10-02T10:00:00",
    client_session_id: "turno-1",
    items: [{ product_id: "p1", quantity: 1, discount_ars: "0.00" }],
    tenders: [{ payment_method: "cash", amount_ars: "100.00" }],
  };
}

async function sembrar(id: string, terminalId: string | null, extra: Record<string, unknown> = {}) {
  await guardarOperacion(
    {
      clientOperationId: id,
      tenantId: "t1",
      userId: "u1",
      terminalId,
      payload: payload(id),
      lines: [],
      recibo,
      ...extra,
    },
    { sinPrimerEnvio: true },
  );
}

async function op(id: string) {
  return (await listarOperaciones("t1")).find((o) => o.clientOperationId === id)!;
}

function rechazo(status: number, code: string, message: string): AxiosError {
  const e = new AxiosError(`status ${status}`);
  e.response = {
    status,
    data: { detail: { code, message } },
    headers: {},
    statusText: "",
    config: {} as InternalAxiosRequestConfig,
  };
  return e;
}

beforeEach(async () => {
  jest.clearAllMocks();
  await _limpiarPosDb();
});

describe("la cola", () => {
  it("una venta de la caja anterior NO sube con la caja nueva", async () => {
    await sembrar("op-1", "caja-vieja");
    const { result } = renderHook(() =>
      usePosSync({ tenantId: "t1", userId: "u1", terminalId: "caja-nueva", automatico: false }),
    );
    await act(async () => {
      await result.current.flush();
    });
    expect(svc.syncOperation).not.toHaveBeenCalled();
  });

  it("después de reasignarla sube UNA vez", async () => {
    await sembrar("op-1", "caja-vieja");
    expect(await reasignarTerminal("t1", "caja-vieja", "caja-nueva", ["op-1"])).toBe(1);
    svc.syncOperation.mockResolvedValue({ id: "srv-1" } as never);
    const { result } = renderHook(() =>
      usePosSync({ tenantId: "t1", userId: "u1", terminalId: "caja-nueva", automatico: false }),
    );
    await act(async () => {
      await result.current.flush();
    });
    await act(async () => {
      await result.current.flush();
    });
    expect(svc.syncOperation).toHaveBeenCalledTimes(1);
    expect((await op("op-1")).estado).toBe("SINCRONIZADA");
  });

  it("la que esperaba autenticación (caja dada de baja) vuelve a pendiente", async () => {
    await sembrar("op-1", "caja-vieja");
    const { actualizarOperacion } = await import("@/lib/offline/posDb");
    await actualizarOperacion("op-1", { estado: "REQUIERE_AUTENTICACION" });
    await reasignarTerminal("t1", "caja-vieja", "caja-nueva", ["op-1"]);
    const guardada = await op("op-1");
    expect(guardada.terminalId).toBe("caja-nueva");
    expect(guardada.estado).toBe("PENDIENTE");
  });

  it("no mueve lo que no se autorizó: otra caja, otro negocio, sincronizadas ni en corrección", async () => {
    await sembrar("otra-caja", "caja-otra");
    await sembrar("en-correccion", "caja-vieja");
    const { actualizarOperacion } = await import("@/lib/offline/posDb");
    await actualizarOperacion("en-correccion", { estado: "EN_CORRECCION" });
    await sembrar("subida", "caja-vieja");
    await actualizarOperacion("subida", { estado: "SINCRONIZADA" });

    const movidas = await reasignarTerminal("t1", "caja-vieja", "caja-nueva", [
      "otra-caja",
      "en-correccion",
      "subida",
      "no-existe",
    ]);
    expect(movidas).toBe(0);
    expect((await op("otra-caja")).terminalId).toBe("caja-otra");
    expect((await op("en-correccion")).terminalId).toBe("caja-vieja");
    expect((await op("subida")).terminalId).toBe("caja-vieja");
    expect(await reasignarTerminal("t2", "caja-otra", "caja-nueva", ["otra-caja"])).toBe(0);
  });

  it("deOtraCaja deja afuera las de esta caja, las subidas y las en corrección", async () => {
    await sembrar("propia", "caja-nueva");
    await sembrar("ajena", "caja-vieja");
    await sembrar("sin-caja", null);
    const ajenas = deOtraCaja(await listarOperaciones("t1"), "caja-nueva");
    expect(ajenas.map((o) => o.clientOperationId).sort()).toEqual(["ajena", "sin-caja"]);
  });
});

describe("el panel", () => {
  async function montar(esDuenio: boolean) {
    const pendientes = await listarOperaciones("t1");
    const onFlush = jest.fn();
    const onCambio = jest.fn();
    render(
      <PendingOperationsPanel
        pendientes={pendientes}
        terminalId="caja-nueva"
        esDuenio={esDuenio}
        sincronizando={false}
        persistencia={true}
        onFlush={onFlush}
        onCambio={onCambio}
        onImprimir={jest.fn()}
      />,
    );
    return { onFlush };
  }

  it("el dueño las pasa: primero el servidor, después la PC, y dispara la subida", async () => {
    await sembrar("op-1", "caja-vieja");
    await sembrar("op-2", "caja-vieja");
    svc.adoptPendingOperations.mockResolvedValue({ terminal_id: "caja-nueva", adopted: 2 });
    const { onFlush } = await montar(true);

    expect(screen.getByText(/2 ventas se cobraron en la caja anterior/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Pasarlas a esta caja" }));

    await waitFor(() => expect(onFlush).toHaveBeenCalled());
    expect(svc.adoptPendingOperations).toHaveBeenCalledWith("caja-vieja", ["op-1", "op-2"]);
    expect((await op("op-1")).terminalId).toBe("caja-nueva");
    expect((await op("op-2")).terminalId).toBe("caja-nueva");
  });

  it("si el servidor no lo autoriza, no se reescribe nada y se dice por qué", async () => {
    await sembrar("op-1", "caja-vieja");
    svc.adoptPendingOperations.mockRejectedValue(
      rechazo(409, "PREVIOUS_TERMINAL_ACTIVE", "La caja anterior sigue habilitada."),
    );
    const { onFlush } = await montar(true);
    fireEvent.click(screen.getByRole("button", { name: "Pasarlas a esta caja" }));

    expect(await screen.findByText("La caja anterior sigue habilitada.")).toBeInTheDocument();
    expect((await op("op-1")).terminalId).toBe("caja-vieja");
    expect(onFlush).not.toHaveBeenCalled();
  });

  it("un cajero no puede pasarlas: se le dice que lo haga el dueño", async () => {
    await sembrar("op-1", "caja-vieja");
    await montar(false);
    expect(screen.queryByRole("button", { name: "Pasarlas a esta caja" })).toBeNull();
    expect(screen.getByText(/Pedile al dueño/)).toBeInTheDocument();
  });
});
