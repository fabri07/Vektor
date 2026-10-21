/**
 * El motor que sube las ventas cobradas.
 *
 * Lo que se protege: que un corte de red no queme el presupuesto de reintentos,
 * que una venta cobrada NUNCA se borre por una respuesta, y que un 402 o un 423
 * —que se arreglan solos— no la manden a revisión.
 */
import * as Sentry from "@sentry/nextjs";
import { act, renderHook, waitFor } from "@testing-library/react";
import { AxiosError, CanceledError, type InternalAxiosRequestConfig } from "axios";

import { usePosSync } from "@/features/pos/usePosSync";
import {
  _limpiarPosDb,
  actualizarOperacion,
  guardarOperacion,
  listarOperaciones,
} from "@/lib/offline/posDb";
import type { PosOperationPayload } from "@/lib/pos/cart";
import type { PosReceipt } from "@/features/pos/ticketTemplate";
import { posService } from "@/services/pos.service";

jest.mock("@sentry/nextjs", () => ({ captureException: jest.fn() }));
const sentry = Sentry as unknown as { captureException: jest.Mock };

jest.mock("@/services/pos.service", () => ({
  posService: { syncOperation: jest.fn() },
}));
const svc = posService as jest.Mocked<typeof posService>;

const recibo = { status: "PENDIENTE" } as unknown as PosReceipt;

function payload(id: string): PosOperationPayload {
  return {
    client_operation_id: id,
    customer_id: null,
    operation_date: "2026-09-27T10:00:00",
    discount_ars: "0.00",
    cash_received_ars: null,
    charged_at: "2026-09-27T10:00:00",
    client_session_id: "turno-1",
    items: [{ product_id: "p1", quantity: 1, discount_ars: "0.00" }],
    tenders: [{ payment_method: "cash", amount_ars: "100.00" }],
  };
}

async function sembrar(id: string, extra: Record<string, unknown> = {}) {
  await guardarOperacion(
    {
      clientOperationId: id,
      tenantId: "t1",
      userId: "u1",
      terminalId: "caja-1",
      payload: payload(id),
      lines: [],
      recibo,
      ...extra,
    },
    // Estos tests son sobre la COLA: la venta ya pasó por su primer envío, así
    // que no le corresponde el resguardo que evita que el motor se adelante.
    { sinPrimerEnvio: true },
  );
}

function httpError(status: number, code?: string): AxiosError {
  const e = new AxiosError(`status ${status}`);
  e.response = {
    status,
    data: code ? { detail: { code } } : {},
    headers: {},
    statusText: "",
    config: {} as InternalAxiosRequestConfig,
  };
  return e;
}

function montar() {
  return renderHook(() =>
    usePosSync({ tenantId: "t1", userId: "u1", terminalId: "caja-1", automatico: false }),
  );
}

async function estado(id: string) {
  return (await listarOperaciones("t1")).find((o) => o.clientOperationId === id)!;
}

beforeEach(async () => {
  jest.clearAllMocks();
  await _limpiarPosDb();
});

it("sube la venta y la deja sincronizada", async () => {
  await sembrar("op-1");
  svc.syncOperation.mockResolvedValue({ id: "srv-1" } as never);
  const { result } = montar();
  await act(async () => {
    await result.current.flush();
  });
  // Una venta de B7 lleva su `charged_at` desde el primer envío, así que
  // sube por el camino offline sin más vueltas.
  expect(svc.syncOperation).toHaveBeenCalledWith(payload("op-1"), { offline: true });
  expect((await estado("op-1")).estado).toBe("SINCRONIZADA");
});

it("un corte de red NO gasta el presupuesto de reintentos", async () => {
  // `navigator.onLine` da true con un router sin internet: si cada intento
  // contara, unos minutos de corte dejarían la venta marcada como problemática
  // sin que el servidor la haya visto nunca (lección de B0).
  await sembrar("op-1");
  svc.syncOperation.mockRejectedValue(new AxiosError("Network Error"));
  const { result } = montar();
  for (let i = 0; i < 3; i += 1) {
    await actualizarOperacion("op-1", { proximoIntentoEn: 0 });
    await act(async () => {
      await result.current.flush();
    });
  }
  const op = await estado("op-1");
  expect(op.intentos).toBe(0);
  expect(op.estado).toBe("PENDIENTE");
});

it("sin red corta la cola en la primera: no gasta el mismo error por venta", async () => {
  await sembrar("op-1");
  await sembrar("op-2");
  svc.syncOperation.mockRejectedValue(new AxiosError("Network Error"));
  const { result } = montar();
  await act(async () => {
    await result.current.flush();
  });
  expect(svc.syncOperation).toHaveBeenCalledTimes(1);
});

it.each([
  [402, "SUBSCRIPTION_READ_ONLY"],
  [423, undefined],
  [503, "SUBSCRIPTION_UNAVAILABLE"],
  [500, undefined],
])("un %s deja la venta pendiente, no en revisión", async (status, code) => {
  // Una clasificación por RANGO ("cualquier 4xx") trataría el 402 y el 423
  // como rechazo definitivo. Para una venta ya cobrada eso es mandarla a
  // revisión por algo que se arregla cuando el dueño paga o termina el
  // mantenimiento. Es la misma razón por la que el primer envío tampoco la
  // borra (ver `pos_screen.test.tsx`).
  await sembrar("op-1");
  svc.syncOperation.mockRejectedValue(httpError(status as number, code as string | undefined));
  const { result } = montar();
  await act(async () => {
    await result.current.flush();
  });
  const op = await estado("op-1");
  expect(op.estado).toBe("PENDIENTE");
  expect(op.intentos).toBe(1);
});

it("una caja dada de baja pide volver a entrar, no revisión", async () => {
  await sembrar("op-1");
  svc.syncOperation.mockRejectedValue(httpError(403, "TERMINAL_DISABLED"));
  const { result } = montar();
  await act(async () => {
    await result.current.flush();
  });
  expect((await estado("op-1")).estado).toBe("REQUIERE_AUTENTICACION");
});

it("un rechazo del servidor va a revisión y la venta NO se borra", async () => {
  await sembrar("op-1");
  svc.syncOperation.mockRejectedValue(httpError(400, "INSUFFICIENT_STOCK"));
  const { result } = montar();
  await act(async () => {
    await result.current.flush();
  });
  const op = await estado("op-1");
  expect(op.estado).toBe("REQUIERE_REVISION");
  expect(op.ultimoCodigo).toBe("INSUFFICIENT_STOCK");
  expect(await listarOperaciones("t1")).toHaveLength(1);
});

it("no sube la venta de otro cajero: el servidor se la adjudicaría a quien esté logueado", async () => {
  await sembrar("de-juan", { userId: "u2" });
  const { result } = montar();
  await act(async () => {
    await result.current.flush();
  });
  expect(svc.syncOperation).not.toHaveBeenCalled();
  expect((await estado("de-juan")).estado).toBe("PENDIENTE");
});

it("volver a entrar reactiva lo que esperaba autenticación", async () => {
  await sembrar("op-1");
  await actualizarOperacion("op-1", { estado: "REQUIERE_AUTENTICACION" });
  svc.syncOperation.mockResolvedValue({ id: "srv-1" } as never);
  montar();
  await waitFor(async () => {
    expect((await estado("op-1")).estado).toBe("SINCRONIZADA");
  });
});

describe("la venta migrada de B13 se reclama antes de adaptarse", () => {
  /** Como la dejó la migración: sin `charged_at`, marcada `origen: "b13"`. */
  async function sembrarB13(id: string) {
    const p = payload(id);
    await sembrar(id, { origen: "b13", payload: { ...p, charged_at: null } });
  }

  it("el primer intento va con el contenido ORIGINAL y sin el header offline", async () => {
    // Su primer envío YA ocurrió y se perdió la respuesta: la venta puede
    // existir en el servidor, y lo único que devuelve su ticket es mandar
    // exactamente lo mismo. `charged_at` entra en la huella de idempotencia
    // (`hash_peticion` usa `exclude_defaults=True`), así que agregarlo acá
    // daría 409 `IDEMPOTENCY_KEY_REUSED` sobre una venta que sí existe.
    await sembrarB13("op-b13");
    svc.syncOperation.mockResolvedValue({ id: "srv-1" } as never);
    const { result } = montar();
    await act(async () => {
      await result.current.flush();
    });
    expect(svc.syncOperation).toHaveBeenCalledWith(
      expect.objectContaining({ client_operation_id: "op-b13", charged_at: null }),
      { offline: false },
    );
    expect((await estado("op-b13")).estado).toBe("SINCRONIZADA");
  });

  it("recién con INSUFFICIENT_STOCK —que prueba que no existe— se le pone `charged_at`", async () => {
    // Llegar al chequeo de stock prueba que la clave se reclamó fresca: una ya
    // usada con el mismo contenido se contesta antes, con el ticket original. Y
    // el 400 revierte la transacción, así que la clave queda libre para el
    // intento adaptado.
    await sembrarB13("op-b13");
    svc.syncOperation.mockRejectedValue(httpError(400, "INSUFFICIENT_STOCK"));
    const { result } = montar();
    await act(async () => {
      await result.current.flush();
    });
    const op = await estado("op-b13");
    expect(op.payload.charged_at).toBe("2026-09-27T10:00:00");
    expect(op.origen).toBeUndefined();
    expect(op.estado).toBe("PENDIENTE");
  });

  it("B·5e-2: SALE_WITHOUT_PURCHASE también prueba que no existe y la adapta", async () => {
    // Sin esto, la venta cobrada quedaba en «Requiere revisión» para siempre:
    // reintentar sin el header devolvía el mismo 422.
    await sembrarB13("op-b13");
    svc.syncOperation.mockRejectedValue(httpError(422, "SALE_WITHOUT_PURCHASE"));
    const { result } = montar();
    await act(async () => {
      await result.current.flush();
    });
    const op = await estado("op-b13");
    expect(op.payload.charged_at).toBe("2026-09-27T10:00:00");
    expect(op.origen).toBeUndefined();
    expect(op.estado).toBe("PENDIENTE");
  });

  it("y el intento siguiente ya va por el camino offline", async () => {
    await sembrarB13("op-b13");
    svc.syncOperation.mockRejectedValueOnce(httpError(400, "INSUFFICIENT_STOCK"));
    svc.syncOperation.mockResolvedValueOnce({ id: "srv-1" } as never);
    const { result } = montar();
    await act(async () => {
      await result.current.flush();
    });
    await act(async () => {
      await result.current.flush();
    });
    expect(svc.syncOperation).toHaveBeenNthCalledWith(2, expect.any(Object), {
      offline: true,
    });
    expect((await estado("op-b13")).estado).toBe("SINCRONIZADA");
  });

  it("un rechazo que NO es de stock no le toca el cuerpo", async () => {
    // Un 422 de otra cosa no prueba nada sobre si la venta existe: adaptar el
    // cuerpo ahí quemaría la única chance de recuperar su ticket.
    await sembrarB13("op-b13");
    svc.syncOperation.mockRejectedValue(httpError(422, "PRICE_TOKEN_INVALID"));
    const { result } = montar();
    await act(async () => {
      await result.current.flush();
    });
    const op = await estado("op-b13");
    expect(op.payload.charged_at).toBeNull();
    expect(op.origen).toBe("b13");
    expect(op.estado).toBe("REQUIERE_REVISION");
  });
});

describe("sin respuesta, sin inventar la causa", () => {
  it("con la PC conectada no dice «Sin conexión», y guarda el tipo y la referencia", async () => {
    // El incidente de 2026-10: un preflight de CORS rechazado se veía igual
    // que un corte, y la caja mandaba a revisar el router con internet andando.
    await sembrar("op-1");
    const e = new AxiosError("Network Error", "ERR_NETWORK", {
      headers: { "X-Trace-Id": "3f2a9c1d-0000" },
    } as unknown as InternalAxiosRequestConfig);
    svc.syncOperation.mockRejectedValue(e);
    const { result } = montar();
    await act(async () => {
      await result.current.flush();
    });
    const op = await estado("op-1");
    expect(op.estado).toBe("PENDIENTE");
    expect(op.intentos).toBe(0);
    expect(op.ultimoError).toBe("No se pudo contactar al servidor");
    expect(op.ultimoTipo).toBe("transporte");
    expect(op.ultimoTraceId).toBe("3f2a9c1d-0000");
  });

  it("un timeout se dice como timeout y tampoco gasta intentos", async () => {
    await sembrar("op-1");
    svc.syncOperation.mockRejectedValue(new AxiosError("timeout", "ECONNABORTED"));
    const { result } = montar();
    await act(async () => {
      await result.current.flush();
    });
    const op = await estado("op-1");
    expect(op.intentos).toBe(0);
    expect(op.ultimoError).toBe("El servidor no respondió a tiempo");
    expect(op.ultimoTipo).toBe("timeout");
  });

  it("con el navegador desconectado sí dice «Sin conexión»", async () => {
    const onLine = jest.spyOn(window.navigator, "onLine", "get").mockReturnValue(false);
    try {
      await sembrar("op-1");
      svc.syncOperation.mockRejectedValue(new AxiosError("Network Error", "ERR_NETWORK"));
      const { result } = montar();
      await act(async () => {
        await result.current.flush();
      });
      expect((await estado("op-1")).ultimoError).toBe("Sin conexión");
    } finally {
      onLine.mockRestore();
    }
  });

  it("una cancelación no es un intento ni cambia el motivo", async () => {
    await sembrar("op-1");
    await actualizarOperacion("op-1", { ultimoError: "motivo anterior" });
    svc.syncOperation.mockRejectedValue(new CanceledError());
    const { result } = montar();
    await act(async () => {
      await result.current.flush();
    });
    const op = await estado("op-1");
    expect(op.estado).toBe("PENDIENTE");
    expect(op.intentos).toBe(0);
    expect(op.ultimoError).toBe("motivo anterior");
  });

  it("una excepción del código de la caja va a revisión, a Sentry, y no se reintenta sola", async () => {
    // Reintentarla sola la repetiría cada 5 segundos para siempre con la venta
    // "pendiente" a la vista y sin que nadie se entere.
    await sembrar("op-1");
    svc.syncOperation.mockRejectedValue(new TypeError("x is undefined"));
    const { result } = montar();
    await act(async () => {
      await result.current.flush();
    });
    const op = await estado("op-1");
    expect(op.estado).toBe("REQUIERE_REVISION");
    expect(op.intentos).toBe(1);
    expect(op.ultimoTipo).toBe("local");
    expect(op.ultimoError).toBe("Error interno de la caja");
    expect(sentry.captureException).toHaveBeenCalledTimes(1);

    // No vuelve a intentarse sola.
    svc.syncOperation.mockClear();
    await act(async () => {
      await result.current.flush();
    });
    expect(svc.syncOperation).not.toHaveBeenCalled();
  });
});
