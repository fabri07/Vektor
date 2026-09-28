/**
 * El catálogo guardado en la PC y el stock estimado.
 */
import {
  catalogoDeCaja,
  nombreDelNegocio,
  sincronizarCatalogo,
  ultimaSincronizacion,
} from "@/lib/offline/catalogDb";
import { _limpiarPosDb, actualizarOperacion, guardarOperacion } from "@/lib/offline/posDb";
import type { PosProduct } from "@/lib/pos/cart";
import type { PosReceipt } from "@/features/pos/ticketTemplate";

function producto(id: string, stock = 10): PosProduct {
  return {
    id,
    name: `Producto ${id}`,
    sale_price_ars: "100.00",
    sale_unit: "unit",
    base_units_per_sale_unit: 1,
    stock_units: stock,
    price_token: `tok-${id}`,
  };
}

const recibo = { status: "PENDIENTE" } as unknown as PosReceipt;

async function venta(id: string, productId: string, cantidad: number) {
  await guardarOperacion({
    clientOperationId: id,
    tenantId: "t1",
    userId: "u1",
    terminalId: null,
    payload: {
      client_operation_id: id,
      customer_id: null,
      operation_date: "x",
      discount_ars: "0.00",
      cash_received_ars: null,
      charged_at: null,
      client_session_id: null,
      items: [{ product_id: productId, quantity: cantidad, discount_ars: "0.00" }],
      tenders: [{ payment_method: "cash", amount_ars: "100.00" }],
    },
    lines: [],
    recibo,
  });
}

beforeEach(async () => {
  await _limpiarPosDb();
});

describe("bajar el catálogo", () => {
  it("recorre todas las páginas por cursor", async () => {
    const paginas = [
      { items: [producto("a"), producto("b")], next_cursor: "b", business_name: "Kiosco" },
      { items: [producto("c")], next_cursor: null, business_name: "Kiosco" },
    ];
    let i = 0;
    const total = await sincronizarCatalogo("t1", async () => paginas[i++]!);
    expect(total).toBe(3);
    expect((await catalogoDeCaja("t1")).map((p) => p.id).sort()).toEqual(["a", "b", "c"]);
    expect(await nombreDelNegocio("t1")).toBe("Kiosco");
    expect(await ultimaSincronizacion("t1")).not.toBeNull();
  });

  it("lo que ya no viene se borra: así se reflejan las bajas", async () => {
    // El servidor no lleva una lista de borrados; el reemplazo completo es lo
    // que hace que un producto dado de baja deje de poder escanearse.
    await sincronizarCatalogo("t1", async () => ({
      items: [producto("a"), producto("b")],
      next_cursor: null,
      business_name: "Kiosco",
    }));
    await sincronizarCatalogo("t1", async () => ({
      items: [producto("a")],
      next_cursor: null,
      business_name: "Kiosco",
    }));
    expect((await catalogoDeCaja("t1")).map((p) => p.id)).toEqual(["a"]);
  });

  it("el catálogo de otro negocio no se mezcla", async () => {
    await sincronizarCatalogo("t1", async () => ({
      items: [producto("a")],
      next_cursor: null,
      business_name: "Kiosco",
    }));
    await sincronizarCatalogo("t2", async () => ({
      items: [producto("z")],
      next_cursor: null,
      business_name: "Otro",
    }));
    expect((await catalogoDeCaja("t1")).map((p) => p.id)).toEqual(["a"]);
  });
});

describe("stock estimado", () => {
  it("es el del servidor menos lo cobrado y no subido", async () => {
    await sincronizarCatalogo("t1", async () => ({
      items: [producto("p1", 10)],
      next_cursor: null,
      business_name: "Kiosco",
    }));
    await venta("op-1", "p1", 3);
    expect((await catalogoDeCaja("t1"))[0]!.stock_estimado).toBe(7);
  });

  it("NO se resta dos veces cuando vuelve a bajar el catálogo", async () => {
    // El bug que evita: si el descuento se acumulara en el producto guardado Y
    // además se derivara de los pendientes, cada sincronización volvería a
    // restar la misma venta.
    await sincronizarCatalogo("t1", async () => ({
      items: [producto("p1", 10)],
      next_cursor: null,
      business_name: "Kiosco",
    }));
    await venta("op-1", "p1", 3);
    await sincronizarCatalogo("t1", async () => ({
      items: [producto("p1", 10)],
      next_cursor: null,
      business_name: "Kiosco",
    }));
    expect((await catalogoDeCaja("t1"))[0]!.stock_estimado).toBe(7);
  });

  it("cuando la venta sube, el estimado vuelve al del servidor", async () => {
    await sincronizarCatalogo("t1", async () => ({
      items: [producto("p1", 10)],
      next_cursor: null,
      business_name: "Kiosco",
    }));
    await venta("op-1", "p1", 3);
    await actualizarOperacion("op-1", { estado: "SINCRONIZADA" });
    // El servidor ya descontó: su número nuevo es el que manda.
    await sincronizarCatalogo("t1", async () => ({
      items: [producto("p1", 7)],
      next_cursor: null,
      business_name: "Kiosco",
    }));
    expect((await catalogoDeCaja("t1"))[0]!.stock_estimado).toBe(7);
  });
});

describe("una PC usada por dos negocios", () => {
  it("cada negocio tiene su nombre y su fecha de sincronización", async () => {
    // Con claves globales, el ticket que la caja imprime SIN RED salía con el
    // nombre del otro local, y la fecha decía "hace 2 minutos" sobre un
    // catálogo que no era el que se estaba mostrando.
    await sincronizarCatalogo("t1", async () => ({
      items: [producto("a")],
      next_cursor: null,
      business_name: "Kiosco Uno",
    }));
    await sincronizarCatalogo("t2", async () => ({
      items: [producto("b")],
      next_cursor: null,
      business_name: "Kiosco Dos",
    }));
    expect(await nombreDelNegocio("t1")).toBe("Kiosco Uno");
    expect(await nombreDelNegocio("t2")).toBe("Kiosco Dos");
    expect(await ultimaSincronizacion("t1")).not.toBeNull();
    expect(await ultimaSincronizacion("t2")).not.toBeNull();
  });
});

describe("el tope de páginas no puede truncar el catálogo", () => {
  it("un cursor que no se agota falla y NO reemplaza nada", async () => {
    // El tope existe para que un cursor que no avanza no cuelgue la caja. Pero
    // si se dispara, no se vio el catálogo entero: reemplazarlo igual borraría
    // como "bajas" a todo lo que quedó del otro lado, y el escáner dejaría de
    // encontrarlo. Es el mismo caso que una página que falla.
    await sincronizarCatalogo("t1", async () => ({
      items: [producto("viejo")],
      next_cursor: null,
      business_name: "Kiosco",
    }));
    let pedidas = 0;
    await expect(
      sincronizarCatalogo("t1", async () => {
        pedidas += 1;
        return { items: [producto(`p${pedidas}`)], next_cursor: `p${pedidas}` };
      }),
    ).rejects.toThrow(/no terminó de bajar/);
    // El catálogo viejo sigue completo y escaneable.
    expect((await catalogoDeCaja("t1")).map((p) => p.id)).toEqual(["viejo"]);
  });
});
