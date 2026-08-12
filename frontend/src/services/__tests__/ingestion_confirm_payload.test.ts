/**
 * Qué manda de verdad el confirm por la red.
 *
 * F-H3.e existió porque el frontend **no mandaba `inventory_effect`**: todo el
 * eje estaba cableado y probado punta a punta contra el endpoint, pero el
 * payload real no lo llevaba. Los tests del panel mockean `ingestionService`
 * entero —verifican el argumento que le pasa el componente, no el cuerpo del
 * POST—, así que el agujero vivía justo en el tramo que nadie miraba.
 *
 * **F-F.4**: el efecto dejó de elegirse y la pantalla dejó de mandarlo (lo
 * deduce el backend). El servicio lo sigue reenviando si alguien se lo pasa,
 * porque el endpoint lo sigue aceptando; los tests de acá fijan ese reenvío como
 * compatibilidad, no como el camino de la pantalla — ése es el que manda `null`.
 */
import { ingestionService } from "../ingestion.service";
import { api } from "@/lib/api";

jest.mock("@/lib/api", () => ({
  api: { post: jest.fn() },
}));

const mockPost = api.post as jest.Mock;

beforeEach(() => {
  jest.clearAllMocks();
  mockPost.mockResolvedValue({ data: { file_id: "f1", status: "ok", message: "" } });
});

function bodyDelConfirm(): Record<string, unknown> {
  const [, body] = mockPost.mock.calls[0] as [string, Record<string, unknown>];
  return body;
}

describe("confirmFile — cuerpo del POST", () => {
  test("compatibilidad: si alguien pasa un efecto, el servicio lo reenvía", async () => {
    await ingestionService.confirmFile(
      "f1",
      { ventas: true },
      [{ source_column: "Monto", target_field: "amount", context_id: "table" }],
      undefined,
      undefined,
      undefined,
      undefined,
      { table: "historical_replay" },
    );

    expect(mockPost).toHaveBeenCalledWith(
      "/ingestion/files/f1/confirm",
      expect.objectContaining({
        inventory_effect: { table: "historical_replay" },
      }),
      expect.anything(),
    );
  });

  test("como lo llama la pantalla desde F-F.4: manda null, no un dict vacío", async () => {
    // Es el camino real: el panel ya no pasa el efecto. `null` y `{}` no son lo
    // mismo — un `{}` se lee como "declaré algo y quedó vacío".
    await ingestionService.confirmFile("f1", { ventas: true });

    expect(bodyDelConfirm().inventory_effect).toBeNull();
  });

  test("la decisión sobre envíos sin comprobante viaja por hoja", async () => {
    // F-H6.b: sin esto, la elección del usuario se quedaba en la pantalla y el
    // backend no cobraba ningún envío — exactamente el agujero de F-H3.e.
    await ingestionService.confirmFile(
      "f1",
      { gastos: true },
      [],
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      [{ context_id: "sheet:Compras", action: "una_por_hoja" }],
    );

    expect(bodyDelConfirm().shipping_decisions).toEqual([
      { context_id: "sheet:Compras", action: "una_por_hoja" },
    ]);
  });

  test("sin decisión de envío manda una lista vacía", async () => {
    // Vacío y "no mandé nada" significan lo mismo para el backend: ninguna hoja
    // declaró qué hacer, así que no se cobra ningún envío sin comprobante.
    await ingestionService.confirmFile("f1", { gastos: true });

    expect(bodyDelConfirm().shipping_decisions).toEqual([]);
  });

  test("el efecto no pisa el tratamiento de stock: son dos ejes", async () => {
    // `stock_treatment` decide si el stock inicial genera un gasto (contable) y
    // **se sigue eligiendo**; `inventory_effect` es el otro eje, el de unidades,
    // que desde F-F.4 se deduce. Mandar uno no puede silenciar al otro.
    await ingestionService.confirmFile(
      "f1",
      { productos: true },
      [],
      undefined,
      undefined,
      { table: "purchase" },
      undefined,
      { table: "current_snapshot" },
    );

    const body = bodyDelConfirm();
    expect(body.stock_treatment).toEqual({ table: "purchase" });
    expect(body.inventory_effect).toEqual({ table: "current_snapshot" });
  });
});

describe("F-H6.c — la decisión de costo viaja por la red", () => {
  test("el confirm lleva purchase_cost_decisions cuando el usuario declaró algo", async () => {
    await ingestionService.confirmFile(
      "f1",
      {},
      [],
      {},
      {},
      undefined,
      undefined,
      undefined,
      undefined,
      [{ context_id: "sheet:Compras", base: "monto_sin_ajustes", line_shipping: "al_costo" }],
    );
    expect(bodyDelConfirm().purchase_cost_decisions).toEqual([
      { context_id: "sheet:Compras", base: "monto_sin_ajustes", line_shipping: "al_costo" },
    ]);
  });

  test("el tercer eje —el envío compartido— también viaja en el cuerpo", async () => {
    // F-H6.d: el eje existía en el backend y en el tipo del servicio, pero el
    // panel lo descartaba al armar el payload. Que el tipo lo tenga no prueba
    // nada sobre lo que sale por la red — es el mismo agujero de F-H3.e, y por
    // eso se verifica el cuerpo del POST y no la firma.
    await ingestionService.confirmFile(
      "f1",
      {},
      [],
      {},
      {},
      undefined,
      undefined,
      undefined,
      undefined,
      [
        {
          context_id: "sheet:Compras",
          base: "monto_incluye",
          shared_shipping: "por_subtotal",
          line_shipping: "gasto_aparte",
        },
      ],
    );
    expect(bodyDelConfirm().purchase_cost_decisions).toEqual([
      {
        context_id: "sheet:Compras",
        base: "monto_incluye",
        shared_shipping: "por_subtotal",
        line_shipping: "gasto_aparte",
      },
    ]);
  });

  test("y va como lista vacía cuando no declaró nada", async () => {
    await ingestionService.confirmFile("f1", {}, [], {}, {});
    // Vacío, no `null`: el backend distingue «sin decisiones» de «no mandó el
    // campo», y una lista vacía dice exactamente que cada hoja toma su default.
    expect(bodyDelConfirm().purchase_cost_decisions).toEqual([]);
  });
});
