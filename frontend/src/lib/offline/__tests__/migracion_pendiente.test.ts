/**
 * El puente de B13 a B7: mover el resguardo viejo sin perder un cobro.
 *
 * Lo que se protege acá es lo único que hace falta proteger: **que ninguna
 * venta cobrada desaparezca**. La clave vieja de B13 guardaba UNA operación en
 * `localStorage`, cobrada y sin confirmar, y esta función es su única salida.
 */
import { migrarPendienteDeB13 } from "@/lib/offline/migracionPendiente";
import {
  _limpiarPosDb,
  listarNoSincronizadas,
  obtenerOperacion,
  pendientesSincronizables,
} from "@/lib/offline/posDb";
import type { PosOperationPayload } from "@/lib/pos/cart";

const CLAVE = "vektor-pos-pending-operation";

const DATOS = {
  tenantId: "t1",
  userId: "u1",
  terminalId: "caja-1",
  businessName: "Kiosco",
  cashierName: "Ana",
  terminalName: "Caja 1",
};

function payloadViejo(extra: Partial<PosOperationPayload> = {}): PosOperationPayload {
  return {
    client_operation_id: "op-viejo",
    customer_id: null,
    operation_date: "2026-09-25T10:00:00",
    discount_ars: "0.00",
    cash_received_ars: null,
    // B13 no tenía este campo: así se veía una venta guardada antes de B7.
    charged_at: null,
    client_session_id: null,
    items: [{ product_id: "p1", quantity: 1, discount_ars: "0.00" }],
    tenders: [{ payment_method: "cash", amount_ars: "1000.00" }],
    ...extra,
  };
}

function sembrarClave(tenantId: string | undefined, payload: unknown): void {
  window.localStorage.setItem(
    CLAVE,
    JSON.stringify({ tenantId, payload, lines: [], savedAt: "x" }),
  );
}

beforeEach(async () => {
  window.localStorage.clear();
  await _limpiarPosDb();
});

describe("migrar el pendiente de B13", () => {
  it("la mueve a la cola y borra la clave vieja", async () => {
    sembrarClave("t1", payloadViejo());
    expect(await migrarPendienteDeB13(DATOS)).toBe("op-viejo");
    expect(await obtenerOperacion("op-viejo")).toBeDefined();
    expect(window.localStorage.getItem(CLAVE)).toBeNull();
  });

  it("le pone `charged_at` desde su propia fecha de operación", async () => {
    // Sin esto, el 422 `OFFLINE_WITHOUT_CHARGED_AT` es INCONDICIONAL para todo
    // lo que sube por la cola —alcance el stock o no—: la venta quedaba en una
    // cola que no podía subirla nunca. Y la fecha no se inventa: B13 congelaba
    // el payload entero al apretar Cobrar.
    sembrarClave("t1", payloadViejo());
    await migrarPendienteDeB13(DATOS);
    const guardada = await obtenerOperacion("op-viejo");
    expect(guardada!.payload.charged_at).toBe("2026-09-25T10:00:00");
  });

  it("si ya traía `charged_at`, no se lo toca", async () => {
    sembrarClave("t1", payloadViejo({ charged_at: "2026-09-25T09:30:00" }));
    await migrarPendienteDeB13(DATOS);
    const guardada = await obtenerOperacion("op-viejo");
    expect(guardada!.payload.charged_at).toBe("2026-09-25T09:30:00");
  });

  it("la cola la puede tomar en el acto: su primer envío ya pasó", async () => {
    sembrarClave("t1", payloadViejo());
    await migrarPendienteDeB13(DATOS);
    expect(await pendientesSincronizables("t1", "u1", "caja-1")).toHaveLength(1);
  });

  it("la de OTRO negocio NO se migra y NO se borra", async () => {
    // Una PC compartida por dos locales: esa venta la cobró el otro negocio y
    // espera a que entre su sesión. Borrarla acá —lo que se hacía— es tirar un
    // cobro ajeno, el peor resultado posible de una función que existe para no
    // perder ventas.
    sembrarClave("t2", payloadViejo());
    expect(await migrarPendienteDeB13(DATOS)).toBeNull();
    expect(await listarNoSincronizadas("t1")).toHaveLength(0);
    expect(window.localStorage.getItem(CLAVE)).not.toBeNull();
  });

  it("y sigue estando cuando entra la sesión de ese negocio", async () => {
    sembrarClave("t2", payloadViejo());
    await migrarPendienteDeB13(DATOS);
    expect(await migrarPendienteDeB13({ ...DATOS, tenantId: "t2" })).toBe("op-viejo");
    expect(window.localStorage.getItem(CLAVE)).toBeNull();
  });

  it("sin nada guardado no hace nada", async () => {
    expect(await migrarPendienteDeB13(DATOS)).toBeNull();
  });

  it("un payload sin id no es una venta: la clave se limpia", async () => {
    sembrarClave("t1", { operation_date: "2026-09-25T10:00:00" });
    expect(await migrarPendienteDeB13(DATOS)).toBeNull();
    expect(window.localStorage.getItem(CLAVE)).toBeNull();
  });

  it("un JSON roto no borra nada: puede ser un cobro ilegible", async () => {
    window.localStorage.setItem(CLAVE, "{no es json");
    expect(await migrarPendienteDeB13(DATOS)).toBeNull();
    expect(window.localStorage.getItem(CLAVE)).not.toBeNull();
  });
});
