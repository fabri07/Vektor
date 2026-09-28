/**
 * La cola de la caja: lo que se guarda, lo que se sube y lo que NO se borra.
 */
import {
  _limpiarPosDb,
  actualizarOperacion,
  borrarOperacion,
  guardarOperacion,
  listarNoSincronizadas,
  listarOperaciones,
  MS_DE_PRIMER_ENVIO,
  pendientesSincronizables,
  podarSincronizadas,
  unidadesComprometidas,
} from "@/lib/offline/posDb";
import type { PosOperationPayload } from "@/lib/pos/cart";
import type { PosReceipt } from "@/features/pos/ticketTemplate";

const recibo: PosReceipt = {
  id: "x",
  number: "X",
  business_name: "Kiosco",
  operation_date: "2026-09-27T10:00:00",
  status: "PENDIENTE",
  lines: [],
  tenders: [],
  subtotal_ars: "0.00",
  discount_ars: "0.00",
  total_ars: "0.00",
  cash_received_ars: null,
  cash_change_ars: null,
  customer_name: null,
  cashier_name: null,
  terminal_name: null,
};

function payload(id: string, items: { product_id: string; quantity: number }[]): PosOperationPayload {
  return {
    client_operation_id: id,
    customer_id: null,
    operation_date: "2026-09-27T10:00:00",
    discount_ars: "0.00",
    cash_received_ars: null,
    charged_at: "2026-09-27T10:00:00",
    client_session_id: "turno-1",
    items: items.map((i) => ({ ...i, discount_ars: "0.00" })),
    tenders: [{ payment_method: "cash", amount_ars: "100.00" }],
  };
}

async function sembrar(
  id: string,
  extra: Partial<Parameters<typeof guardarOperacion>[0]> = {},
  items: { product_id: string; quantity: number }[] = [{ product_id: "p1", quantity: 2 }],
  opciones: Parameters<typeof guardarOperacion>[1] = {},
) {
  return guardarOperacion(
    {
      clientOperationId: id,
      tenantId: "t1",
      userId: "u1",
      terminalId: "caja-1",
      payload: payload(id, items),
      lines: [],
      recibo,
      ...extra,
    },
    opciones,
  );
}

beforeEach(async () => {
  await _limpiarPosDb();
});

describe("guardar", () => {
  it("una venta nace PENDIENTE y sin intentos", async () => {
    const op = await sembrar("op-1");
    expect(op.estado).toBe("PENDIENTE");
    expect(op.intentos).toBe(0);
    expect((await listarOperaciones("t1"))).toHaveLength(1);
  });

  it("las lista en orden de cobro", async () => {
    await sembrar("op-1");
    await new Promise((r) => setTimeout(r, 2));
    await sembrar("op-2");
    expect((await listarOperaciones("t1")).map((o) => o.clientOperationId)).toEqual([
      "op-1",
      "op-2",
    ]);
  });
});

describe("a quién le pertenece una venta", () => {
  it("una venta de otro negocio, otra caja u otro cajero no se sincroniza", async () => {
    await sembrar("mia");
    await sembrar("otro-negocio", { tenantId: "t2" });
    await sembrar("otro-cajero", { userId: "u2" });
    await sembrar("otra-caja", { terminalId: "caja-2" });

    // Pasado el resguardo del primer envío, la propia es la única sincronizable.
    const cola = await pendientesSincronizables(
      "t1", "u1", "caja-1", Date.now() + MS_DE_PRIMER_ENVIO + 1,
    );
    expect(cola.map((o) => o.clientOperationId)).toEqual(["mia"]);
  });

  it("una venta con el reintento agendado para más tarde no se toma todavía", async () => {
    await sembrar("op-1");
    await actualizarOperacion("op-1", { proximoIntentoEn: Date.now() + 60_000 });
    expect(await pendientesSincronizables("t1", "u1", "caja-1")).toHaveLength(0);
  });
});

describe("el primer envío y la cola no se pisan", () => {
  it("una venta recién guardada NO se sincroniza: su primer envío sigue en vuelo", async () => {
    await sembrar("op-1");
    // El motor periódico corre cada 20 s y ante cada `online`: sin este
    // resguardo tomaba la venta mientras el POST original seguía abierto y la
    // reenviaba con `X-POS-Offline: 1`. La idempotencia evita el ticket
    // duplicado, pero si gana ese envío se CAPTURA stock que el intento online
    // habría rechazado para que el cajero decidiera.
    expect(await pendientesSincronizables("t1", "u1", "caja-1")).toHaveLength(0);
  });

  it("si el proceso muere en el medio, la venta se destraba sola", async () => {
    await sembrar("op-1");
    const despues = Date.now() + MS_DE_PRIMER_ENVIO + 1;
    const cola = await pendientesSincronizables("t1", "u1", "caja-1", despues);
    expect(cola.map((o) => o.clientOperationId)).toEqual(["op-1"]);
  });

  it("la venta migrada de B13 se toma en el acto: su envío ya pasó", async () => {
    await sembrar("vieja", {}, undefined, { sinPrimerEnvio: true });
    expect(await pendientesSincronizables("t1", "u1", "caja-1")).toHaveLength(1);
  });
});

describe("nada se borra solo", () => {
  it("una venta rechazada cambia de estado pero sigue guardada", async () => {
    await sembrar("op-1");
    await actualizarOperacion("op-1", {
      estado: "REQUIERE_REVISION",
      ultimoError: "sin stock",
    });
    const guardadas = await listarNoSincronizadas("t1");
    expect(guardadas).toHaveLength(1);
    expect(guardadas[0]!.estado).toBe("REQUIERE_REVISION");
  });

  it("borrar es explícito", async () => {
    await sembrar("op-1");
    await borrarOperacion("op-1");
    expect(await listarOperaciones("t1")).toHaveLength(0);
  });

  it("una sincronizada deja de contar como pendiente", async () => {
    await sembrar("op-1");
    await actualizarOperacion("op-1", { estado: "SINCRONIZADA" });
    expect(await listarNoSincronizadas("t1")).toHaveLength(0);
    expect(await listarOperaciones("t1")).toHaveLength(1);
  });
});

describe("unidades comprometidas", () => {
  it("suma lo vendido y todavía no subido, por producto", async () => {
    await sembrar("op-1", {}, [{ product_id: "p1", quantity: 2 }]);
    await sembrar("op-2", {}, [
      { product_id: "p1", quantity: 3 },
      { product_id: "p2", quantity: 1 },
    ]);
    const mapa = await unidadesComprometidas("t1");
    expect(mapa.get("p1")).toBe(5);
    expect(mapa.get("p2")).toBe(1);
  });

  it("lo ya sincronizado deja de estar comprometido", async () => {
    // Si siguiera contando, el stock estimado restaría dos veces la misma
    // venta: una en el número del servidor y otra acá.
    await sembrar("op-1", {}, [{ product_id: "p1", quantity: 2 }]);
    await actualizarOperacion("op-1", { estado: "SINCRONIZADA" });
    expect((await unidadesComprometidas("t1")).get("p1")).toBeUndefined();
  });
});

describe("podar lo ya subido", () => {
  const SEMANA = 7 * 24 * 60 * 60 * 1000;

  it("borra las sincronizadas viejas y deja las de esta semana", async () => {
    // Una caja con 200 ventas por día acumularía 73.000 filas por año, y el
    // navegador puede evacuar la base por tamaño — llevándose las PENDIENTES,
    // que son las únicas que no están en ningún otro lado.
    const ahora = Date.now();
    await sembrar("vieja");
    await actualizarOperacion("vieja", {
      estado: "SINCRONIZADA",
      sincronizadaEn: new Date(ahora - SEMANA - 60_000).toISOString(),
    });
    await sembrar("reciente");
    await actualizarOperacion("reciente", {
      estado: "SINCRONIZADA",
      sincronizadaEn: new Date(ahora - 60_000).toISOString(),
    });

    expect(await podarSincronizadas("t1", ahora)).toBe(1);
    expect((await listarOperaciones("t1")).map((o) => o.clientOperationId)).toEqual([
      "reciente",
    ]);
  });

  it("NUNCA borra una venta que no esté sincronizada, por vieja que sea", async () => {
    const ahora = Date.now();
    await sembrar("pendiente-vieja");
    await actualizarOperacion("pendiente-vieja", {
      estado: "REQUIERE_REVISION",
      sincronizadaEn: new Date(ahora - 365 * 24 * 60 * 60 * 1000).toISOString(),
    });
    expect(await podarSincronizadas("t1", ahora)).toBe(0);
    expect(await listarOperaciones("t1")).toHaveLength(1);
  });
});
