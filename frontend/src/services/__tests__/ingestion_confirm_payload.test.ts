/**
 * Qué manda de verdad el confirm por la red.
 *
 * F-H3.e existió porque el frontend **no mandaba `inventory_effect`**: todo el
 * eje estaba cableado y probado punta a punta contra el endpoint, pero el
 * payload real no lo llevaba, así que `historical_replay` —el único modo que
 * escribe stock— era inalcanzable desde la pantalla.
 *
 * Los tests del panel mockean `ingestionService` entero: verifican el argumento
 * que le pasa el componente, no el cuerpo del POST. Este cubre el otro tramo, que
 * es donde estaba el agujero.
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
  test("el efecto de inventario viaja tal como se eligió, por hoja", async () => {
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

  test("sin efecto declarado manda null, no un dict vacío", async () => {
    // Para el backend significan lo mismo (cada hoja toma su default), pero
    // `null` lo dice: un `{}` se lee como "declaré algo y quedó vacío".
    await ingestionService.confirmFile("f1", { ventas: true });

    expect(bodyDelConfirm().inventory_effect).toBeNull();
  });

  test("el efecto no pisa el tratamiento de stock: son dos ejes", async () => {
    // `stock_treatment` decide si el stock inicial genera un gasto (contable);
    // `inventory_effect`, qué pasa con las unidades. Mandar uno no puede
    // silenciar al otro.
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
