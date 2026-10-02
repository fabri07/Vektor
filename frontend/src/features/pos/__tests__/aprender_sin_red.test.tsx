/**
 * Aprender un código SIN RED (cierre de B4).
 *
 * Lo que se protege: que el segundo escaneo lo encuentre sin conexión, que
 * reemplazar el catálogo no borre lo pendiente, que suba UNA vez y marcado como
 * sin red, que un conflicto pase a revisión sin tocar ninguna venta, y que la
 * base de la caja suba de versión sin perder las ventas guardadas.
 */
import { renderHook, act } from "@testing-library/react";
import { AxiosError, type InternalAxiosRequestConfig } from "axios";
import { deleteDB, openDB } from "idb";

import { buscarLocal } from "@/features/pos/ProductPicker";
import { usePosSync } from "@/features/pos/usePosSync";
import type { PosReceipt } from "@/features/pos/ticketTemplate";
import {
  guardarAprendizaje,
  listarAprendizajes,
} from "@/lib/offline/aprendizajes";
import { catalogoDeCaja, sincronizarCatalogo } from "@/lib/offline/catalogDb";
import {
  _limpiarPosDb,
  _resetPosDb,
  abrirPosDb,
  listarOperaciones,
} from "@/lib/offline/posDb";
import type { PosProduct } from "@/lib/pos/cart";
import { buscarEnCatalogo, clasificar } from "@/lib/pos/scanCodeLocal";
import { posService } from "@/services/pos.service";

jest.mock("@/services/pos.service", () => ({
  posService: { syncOperation: jest.fn(), learnBarcode: jest.fn() },
}));
const svc = posService as jest.Mocked<typeof posService>;

const EAN = "7790001234568"; // GTIN válido: un verificador mal lo haría OPAQUE

const alfajor: PosProduct = {
  id: "p1",
  name: "Alfajor Triple",
  sale_price_ars: "500.00",
  sale_unit: "unit",
  base_units_per_sale_unit: 1,
  stock_units: 10,
  barcode: null,
  sku: "ALF-3",
};

async function catalogo(productos: PosProduct[]) {
  await sincronizarCatalogo("t1", async () => ({ items: productos, next_cursor: null }));
}

async function aprender() {
  await guardarAprendizaje({
    tenantId: "t1",
    userId: "u1",
    terminalId: "caja-1",
    codigo: EAN,
    productId: alfajor.id,
    productName: alfajor.name,
  });
}

function rechazo(status: number, code: string): AxiosError {
  const e = new AxiosError(`status ${status}`);
  e.response = {
    status,
    data: { detail: { code, message: "Ese código ya corresponde a otro producto." } },
    headers: {},
    statusText: "",
    config: {} as InternalAxiosRequestConfig,
  };
  return e;
}

function sinRed(): AxiosError {
  const e = new AxiosError("Network Error");
  e.code = "ERR_NETWORK";
  return e;
}

function motor() {
  return renderHook(() =>
    usePosSync({ tenantId: "t1", userId: "u1", terminalId: "caja-1", automatico: false }),
  );
}

beforeEach(async () => {
  jest.clearAllMocks();
  await _limpiarPosDb();
});

describe("la superposición", () => {
  it("el segundo escaneo sin red encuentra el código aprendido", async () => {
    await catalogo([alfajor]);
    expect(buscarEnCatalogo(clasificar(EAN), await catalogoDeCaja("t1"))).toHaveLength(0);
    await aprender();
    const [hallado] = buscarEnCatalogo(clasificar(EAN), await catalogoDeCaja("t1"));
    expect(hallado?.producto.id).toBe("p1");
  });

  it("reemplazar el catálogo no borra un aprendizaje pendiente", async () => {
    await catalogo([alfajor]);
    await aprender();
    await catalogo([alfajor]);
    expect((await listarAprendizajes("t1")).map((a) => a.estado)).toEqual(["PENDIENTE"]);
    expect((await catalogoDeCaja("t1"))[0]?.barcode).toBe(EAN);
  });

  it("nunca pisa un código que el producto ya tiene", async () => {
    await catalogo([{ ...alfajor, barcode: "7790009999998" }]);
    await aprender();
    expect((await catalogoDeCaja("t1"))[0]?.barcode).toBe("7790009999998");
  });
});

describe("la subida", () => {
  it("sube UNA vez, marcada como sin red, y el catálogo que ya lo trae lo poda", async () => {
    await catalogo([alfajor]);
    await aprender();
    svc.learnBarcode.mockResolvedValue({ ...alfajor, barcode: EAN });
    const { result } = motor();
    await act(async () => {
      await result.current.flush();
    });
    await act(async () => {
      await result.current.flush();
    });
    expect(svc.learnBarcode).toHaveBeenCalledTimes(1);
    expect(svc.learnBarcode).toHaveBeenCalledWith("p1", EAN, { offline: true });
    expect((await listarAprendizajes("t1"))[0]?.estado).toBe("SINCRONIZADO");

    await catalogo([{ ...alfajor, barcode: EAN }]);
    expect(await listarAprendizajes("t1")).toHaveLength(0);
  });

  it("un código que ya es de otro producto pasa a revisión y deja de superponerse", async () => {
    await catalogo([alfajor]);
    await aprender();
    svc.learnBarcode.mockRejectedValue(rechazo(409, "BARCODE_TAKEN"));
    const { result } = motor();
    await act(async () => {
      await result.current.flush();
    });
    const [a] = await listarAprendizajes("t1");
    expect(a?.estado).toBe("REQUIERE_REVISION");
    expect((await catalogoDeCaja("t1"))[0]?.barcode).toBeNull();
    // Ninguna venta se tocó: la cola de ventas sigue vacía.
    expect(await listarOperaciones("t1")).toHaveLength(0);
  });

  it("un permiso revocado no se reintenta para siempre: va a revisión", async () => {
    await catalogo([alfajor]);
    await aprender();
    svc.learnBarcode.mockRejectedValue(rechazo(403, "POS_PERMISSION_DENIED"));
    const { result } = motor();
    await act(async () => {
      await result.current.flush();
    });
    expect((await listarAprendizajes("t1"))[0]?.estado).toBe("REQUIERE_REVISION");
    expect((await catalogoDeCaja("t1"))[0]?.barcode).toBeNull();
  });

  it("uno subido que el catálogo contradice se poda y no revive", async () => {
    await catalogo([alfajor]);
    await aprender();
    svc.learnBarcode.mockResolvedValue({ ...alfajor, barcode: EAN });
    const { result } = motor();
    await act(async () => {
      await result.current.flush();
    });
    // El dueño le cambió el código después: el vínculo viejo no puede volver.
    await catalogo([{ ...alfajor, barcode: "7790009999998" }]);
    expect(await listarAprendizajes("t1")).toHaveLength(0);
    await catalogo([alfajor]);
    expect((await catalogoDeCaja("t1"))[0]?.barcode).toBeNull();
  });

  it("sin red queda pendiente, sin gastar intentos", async () => {
    await catalogo([alfajor]);
    await aprender();
    svc.learnBarcode.mockRejectedValue(sinRed());
    const { result } = motor();
    await act(async () => {
      await result.current.flush();
    });
    const [a] = await listarAprendizajes("t1");
    expect(a?.estado).toBe("PENDIENTE");
    expect(a?.intentos).toBe(0);
  });

  it("no sube el aprendizaje de otro cajero", async () => {
    await catalogo([alfajor]);
    await guardarAprendizaje({
      tenantId: "t1", userId: "otro", terminalId: "caja-1", codigo: EAN,
      productId: "p1", productName: "Alfajor Triple",
    });
    const { result } = motor();
    await act(async () => {
      await result.current.flush();
    });
    expect(svc.learnBarcode).not.toHaveBeenCalled();
  });
});

describe("buscar sin red", () => {
  it("busca por nombre (sin acentos) o SKU en el catálogo de la PC", () => {
    const productos = [alfajor, { ...alfajor, id: "p2", name: "Café molido", sku: null }];
    expect(buscarLocal(productos, "alfa").map((p) => p.id)).toEqual(["p1"]);
    expect(buscarLocal(productos, "cafe").map((p) => p.id)).toEqual(["p2"]);
    expect(buscarLocal(productos, "alf-3").map((p) => p.id)).toEqual(["p1"]);
    expect(buscarLocal(productos, "a")).toEqual([]);
  });
});

describe("la base de la caja sube de versión", () => {
  it("de v1 a v2 sin perder las ventas guardadas", async () => {
    (await abrirPosDb()).close();
    _resetPosDb();
    await deleteDB("vektor-pos");
    const v1 = await openDB("vektor-pos", 1, {
      upgrade(base) {
        const ops = base.createObjectStore("operaciones", { keyPath: "clientOperationId" });
        ops.createIndex("porEstado", "estado");
        ops.createIndex("porTenant", "tenantId");
        base.createObjectStore("catalogo", { keyPath: "id" }).createIndex("porTenant", "tenantId");
        base.createObjectStore("meta", { keyPath: "clave" });
      },
    });
    await v1.put("operaciones", {
      clientOperationId: "op-viejo",
      tenantId: "t1",
      userId: "u1",
      terminalId: "caja-1",
      estado: "PENDIENTE",
      payload: {},
      lines: [],
      recibo: { status: "PENDIENTE" } as unknown as PosReceipt,
      creadaEn: "2026-10-01T10:00:00.000Z",
      intentos: 0,
      ultimoError: null,
      ultimoCodigo: null,
      proximoIntentoEn: 0,
      resultado: null,
      sincronizadaEn: null,
    });
    v1.close();

    const v2 = await abrirPosDb();
    expect(v2.objectStoreNames.contains("aprendizajes")).toBe(true);
    expect((await listarOperaciones("t1")).map((o) => o.clientOperationId)).toEqual(["op-viejo"]);
  });
});
