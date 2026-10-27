import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

import { useOfflineQueueCount, useOfflineSubmit } from "../useOfflineSubmit";
import { useOfflineQueueStore, type QueuedItem } from "@/stores/offlineQueueStore";
import { useAuthStore } from "@/stores/authStore";
import { salesService } from "@/services/sales.service";

jest.mock("@/services/sales.service", () => ({
  salesService: { createSale: jest.fn() },
}));
jest.mock("@/services/expenses.service", () => ({
  expensesService: { createExpense: jest.fn() },
}));
jest.mock("@/services/products.service", () => ({
  productsService: { createProduct: jest.fn() },
}));

const mockCreateSale = salesService.createSale as jest.Mock;

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function seed(attempts = 0) {
  useOfflineQueueStore.setState({
    items: [
      {
        id: "11111111-1111-1111-1111-111111111111",
        kind: "sale",
        payload: { amount: 1, quantity: 1, transaction_date: "x", payment_method: "cash" },
        createdAt: "2026-06-15T00:00:00Z",
        attempts,
        tenantId: "tenant-a",
        userId: "user-a",
      },
    ],
  });
}

/** Sesión logueada en esta PC: el negocio y el usuario del JWT. */
function entrarComo(tenantId: string, userId: string) {
  useAuthStore.setState({
    user: { id: userId, email: `${userId}@x.com`, full_name: userId, role: "OWNER", tenant_id: tenantId },
  });
}

const axiosErr = (status?: number) => ({
  isAxiosError: true,
  response: status ? { status, data: {} } : undefined,
});

beforeEach(() => {
  useOfflineQueueStore.setState({ items: [] });
  mockCreateSale.mockReset();
  entrarComo("tenant-a", "user-a");
});

describe("useOfflineSubmit.flush poison-item handling", () => {
  it("keeps a 4xx item marked-failed (visible, no silent drop) below the cap", async () => {
    // Una operación transaccional (sale_batch/purchase) puede 400 legítimamente en
    // el replay (sobreventa, proveedor borrado): NO se borra en silencio, queda
    // visible con su lastError hasta el tope de reintentos.
    seed();
    mockCreateSale.mockRejectedValue(axiosErr(400));
    const { result } = renderHook(() => useOfflineSubmit(), { wrapper });
    await act(async () => {
      await result.current.flush();
    });
    const items = useOfflineQueueStore.getState().items;
    expect(items).toHaveLength(1);
    expect(items[0]?.attempts).toBe(1);
    expect(items[0]?.lastError).toBeTruthy();
  });

  it("holds a permanent 4xx item in FAILED at the cap instead of deleting it", async () => {
    // Una venta ya cobrada NO se borra porque el servidor la rechazó 5 veces:
    // frena en estado terminal y queda visible esperando decisión humana.
    seed(4); // next attempt = 5 = MAX_FLUSH_ATTEMPTS
    mockCreateSale.mockRejectedValue(axiosErr(400));
    const { result } = renderHook(() => useOfflineSubmit(), { wrapper });
    await act(async () => {
      await result.current.flush();
    });
    const items = useOfflineQueueStore.getState().items;
    expect(items).toHaveLength(1);
    expect(items[0]?.status).toBe("FAILED");
    expect(items[0]?.lastError).toBeTruthy();
  });

  it("keeps a transient 5xx item for retry (below the attempt cap)", async () => {
    seed(0);
    mockCreateSale.mockRejectedValue(axiosErr(503));
    const { result } = renderHook(() => useOfflineSubmit(), { wrapper });
    await act(async () => {
      await result.current.flush();
    });
    const items = useOfflineQueueStore.getState().items;
    expect(items).toHaveLength(1);
    expect(items[0]?.attempts).toBe(1);
  });

  it("holds a 5xx item in FAILED at the cap instead of deleting it", async () => {
    seed(4); // next attempt = 5 = MAX_FLUSH_ATTEMPTS
    mockCreateSale.mockRejectedValue(axiosErr(503));
    const { result } = renderHook(() => useOfflineSubmit(), { wrapper });
    await act(async () => {
      await result.current.flush();
    });
    const items = useOfflineQueueStore.getState().items;
    expect(items).toHaveLength(1);
    expect(items[0]?.status).toBe("FAILED");
  });

  it("skips a FAILED item on later flushes (no infinite retry)", async () => {
    // El tope sigue cumpliendo su función original —no reintentar para siempre—
    // pero saltando el item, no borrándolo.
    seed(5);
    useOfflineQueueStore.setState((st) => ({
      items: st.items.map((i) => ({ ...i, status: "FAILED" as const })),
    }));
    const { result } = renderHook(() => useOfflineSubmit(), { wrapper });
    await act(async () => {
      await result.current.flush();
    });
    expect(mockCreateSale).not.toHaveBeenCalled();
    expect(useOfflineQueueStore.getState().items).toHaveLength(1);
  });

  it("retry() puts a FAILED item back in the flush rotation", async () => {
    seed(5);
    const id = useOfflineQueueStore.getState().items[0]!.id;
    useOfflineQueueStore.getState().markPermanentlyFailed(id, "boom");
    useOfflineQueueStore.getState().retry(id);
    mockCreateSale.mockResolvedValue({ id: "ok" });
    const { result } = renderHook(() => useOfflineSubmit(), { wrapper });
    await act(async () => {
      await result.current.flush();
    });
    expect(mockCreateSale).toHaveBeenCalledTimes(1);
    expect(useOfflineQueueStore.getState().items).toHaveLength(0);
  });

  it("treats a legacy item with no status as pending (persisted queues)", async () => {
    // Las colas ya en localStorage (version 1) no tienen `status`: arrancarlas
    // como terminales las congelaría para siempre.
    seed(0);
    expect(useOfflineQueueStore.getState().items[0]?.status).toBeUndefined();
    mockCreateSale.mockResolvedValue({ id: "ok" });
    const { result } = renderHook(() => useOfflineSubmit(), { wrapper });
    await act(async () => {
      await result.current.flush();
    });
    expect(mockCreateSale).toHaveBeenCalledTimes(1);
  });

  it("keeps a network-error item (no response) for the next flush", async () => {
    seed();
    mockCreateSale.mockRejectedValue(axiosErr(undefined));
    const { result } = renderHook(() => useOfflineSubmit(), { wrapper });
    await act(async () => {
      await result.current.flush();
    });
    expect(useOfflineQueueStore.getState().items).toHaveLength(1);
  });

  it("a network error does NOT consume the retry budget", async () => {
    // `navigator.onLine` da true con un router sin internet, y flush corre una vez
    // por navegación de página: si la red contara, unos clics durante un corte
    // agotaban el tope y el primer 503 real mandaba la venta a FAILED sin haberla
    // reintentado nunca.
    seed(0);
    mockCreateSale.mockRejectedValue(axiosErr(undefined));
    const { result } = renderHook(() => useOfflineSubmit(), { wrapper });
    for (let i = 0; i < 6; i++) {
      await act(async () => {
        await result.current.flush();
      });
    }
    const items = useOfflineQueueStore.getState().items;
    expect(items).toHaveLength(1);
    expect(items[0]?.attempts).toBe(0);
    expect(items[0]?.status).toBeUndefined();
    expect(items[0]?.lastError).toBe("Sin conexión al sincronizar");
  });

  it("a real 503 after a long outage still gets its full retry budget", async () => {
    // El caso compuesto del hallazgo: corte largo y después un error transitorio.
    seed(0);
    mockCreateSale.mockRejectedValue(axiosErr(undefined));
    const { result } = renderHook(() => useOfflineSubmit(), { wrapper });
    for (let i = 0; i < 4; i++) {
      await act(async () => {
        await result.current.flush();
      });
    }
    mockCreateSale.mockRejectedValue(axiosErr(503));
    await act(async () => {
      await result.current.flush();
    });
    const items = useOfflineQueueStore.getState().items;
    expect(items[0]?.attempts).toBe(1);
    expect(items[0]?.status).toBeUndefined(); // sigue pendiente, no terminal
  });

  it("removes an item on successful sync", async () => {
    seed();
    mockCreateSale.mockResolvedValue({ id: "ok" });
    const { result } = renderHook(() => useOfflineSubmit(), { wrapper });
    await act(async () => {
      await result.current.flush();
    });
    expect(useOfflineQueueStore.getState().items).toHaveLength(0);
  });
});

describe("useOfflineSubmit — la cola es de quien la cargó (X6-1)", () => {
  const ITEM_DE_A: QueuedItem = {
    id: "33333333-3333-3333-3333-333333333333",
    kind: "sale",
    payload: { amount: 1, quantity: 1, transaction_date: "x", payment_method: "cash" },
    createdAt: "2026-10-26T12:00:00Z",
    attempts: 0,
    tenantId: "tenant-a",
    userId: "user-a",
  };

  it("una carga del negocio A NO se manda con la sesión del negocio B", async () => {
    // La idempotencia es por (tenant, clave): mandada con el JWT de B entraba en B.
    useOfflineQueueStore.setState({ items: [ITEM_DE_A] });
    entrarComo("tenant-b", "user-b");
    mockCreateSale.mockResolvedValue({ id: "ok" });
    const { result } = renderHook(() => useOfflineSubmit(), { wrapper });
    await act(async () => {
      await result.current.flush();
    });
    expect(mockCreateSale).not.toHaveBeenCalled();
    // Y sigue en la cola, intacta, esperando a su dueño.
    expect(useOfflineQueueStore.getState().items).toEqual([ITEM_DE_A]);
  });

  it("tampoco con OTRO usuario del mismo negocio", async () => {
    useOfflineQueueStore.setState({ items: [ITEM_DE_A] });
    entrarComo("tenant-a", "user-otro");
    const { result } = renderHook(() => useOfflineSubmit(), { wrapper });
    await act(async () => {
      await result.current.flush();
    });
    expect(mockCreateSale).not.toHaveBeenCalled();
  });

  it("con la sesión de A sí se manda", async () => {
    useOfflineQueueStore.setState({ items: [ITEM_DE_A] });
    mockCreateSale.mockResolvedValue({ id: "ok" });
    const { result } = renderHook(() => useOfflineSubmit(), { wrapper });
    await act(async () => {
      await result.current.flush();
    });
    expect(mockCreateSale).toHaveBeenCalledWith(ITEM_DE_A.payload, ITEM_DE_A.id);
    expect(useOfflineQueueStore.getState().items).toHaveLength(0);
  });

  it("sin nadie logueado no se manda nada", async () => {
    useOfflineQueueStore.setState({ items: [ITEM_DE_A] });
    useAuthStore.setState({ user: null });
    const { result } = renderHook(() => useOfflineSubmit(), { wrapper });
    await act(async () => {
      await result.current.flush();
    });
    expect(mockCreateSale).not.toHaveBeenCalled();
  });

  it("una carga vieja sin dueño NO se sube sola", async () => {
    const { tenantId: _t, userId: _u, ...legado } = ITEM_DE_A;
    useOfflineQueueStore.setState({ items: [legado] });
    mockCreateSale.mockResolvedValue({ id: "ok" });
    const { result } = renderHook(() => useOfflineSubmit(), { wrapper });
    await act(async () => {
      await result.current.flush();
    });
    expect(mockCreateSale).not.toHaveBeenCalled();
    expect(useOfflineQueueStore.getState().items).toHaveLength(1);
  });

  it("adoptarla (acción explícita) la deja sincronizable por esta sesión", async () => {
    const { tenantId: _t, userId: _u, ...legado } = ITEM_DE_A;
    useOfflineQueueStore.setState({ items: [legado] });
    useOfflineQueueStore.getState().adoptar(legado.id, { tenantId: "tenant-a", userId: "user-a" });
    mockCreateSale.mockResolvedValue({ id: "ok" });
    const { result } = renderHook(() => useOfflineSubmit(), { wrapper });
    await act(async () => {
      await result.current.flush();
    });
    expect(mockCreateSale).toHaveBeenCalledTimes(1);
  });

  it("adoptar no le cambia el dueño a una carga que ya lo tiene", () => {
    useOfflineQueueStore.setState({ items: [ITEM_DE_A] });
    useOfflineQueueStore.getState().adoptar(ITEM_DE_A.id, { tenantId: "tenant-b", userId: "user-b" });
    expect(useOfflineQueueStore.getState().items[0]?.tenantId).toBe("tenant-a");
  });

  it("al encolar sin red guarda el negocio y el usuario de la sesión", async () => {
    mockCreateSale.mockRejectedValue(axiosErr(undefined));
    const { result } = renderHook(() => useOfflineSubmit(), { wrapper });
    await act(async () => {
      await result.current.submit("sale", ITEM_DE_A.payload as never);
    });
    const items = useOfflineQueueStore.getState().items;
    expect(items).toHaveLength(1);
    expect(items[0]?.tenantId).toBe("tenant-a");
    expect(items[0]?.userId).toBe("user-a");
  });

  it("el contador sólo cuenta lo de la sesión actual", () => {
    useOfflineQueueStore.setState({
      items: [ITEM_DE_A, { ...ITEM_DE_A, id: "b", tenantId: "tenant-b", userId: "user-b" }],
    });
    const { result } = renderHook(() => useOfflineQueueCount(), { wrapper });
    expect(result.current).toBe(1);
  });
});

describe("offlineQueueStore — migración del persist a v2 (X6-1)", () => {
  it("conserva los items de v1 tal cual, sin inventarles dueño", async () => {
    const legado = {
      id: "44444444-4444-4444-4444-444444444444",
      kind: "sale",
      payload: {},
      createdAt: "2026-06-15T00:00:00Z",
      attempts: 0,
    };
    localStorage.setItem(
      "vektor_offline_queue",
      JSON.stringify({ state: { items: [legado] }, version: 1 }),
    );
    await useOfflineQueueStore.persist.rehydrate();
    const items = useOfflineQueueStore.getState().items;
    expect(items).toEqual([legado]);
    expect(items[0]?.tenantId).toBeUndefined();
  });
});

describe("offlineQueueStore — dos pestañas no se pisan la cola (X6-1)", () => {
  const item = (id: string): QueuedItem => ({
    id,
    kind: "sale",
    payload: {},
    createdAt: "2026-10-26T12:00:00Z",
    attempts: 0,
    tenantId: "tenant-a",
    userId: "user-a",
  });

  /** Lo que haría la OTRA pestaña: escribir la clave sin pasar por esta memoria. */
  function otraPestaniaGuarda(items: QueuedItem[]) {
    localStorage.setItem("vektor_offline_queue", JSON.stringify({ state: { items }, version: 2 }));
  }

  it("encolar no borra lo que encoló la otra pestaña", () => {
    useOfflineQueueStore.setState({ items: [item("1")] });
    otraPestaniaGuarda([item("1"), item("2")]); // esta pestaña sigue viendo sólo "1"
    useOfflineQueueStore.getState().enqueue(item("3"));
    const ids = useOfflineQueueStore.getState().items.map((i) => i.id);
    expect(ids.sort()).toEqual(["1", "2", "3"]);
    const guardado = JSON.parse(localStorage.getItem("vektor_offline_queue")!).state.items;
    expect(guardado).toHaveLength(3);
  });

  it("remove de un item no borra lo que encoló la otra pestaña", () => {
    useOfflineQueueStore.setState({ items: [item("1")] });
    otraPestaniaGuarda([item("1"), item("2")]);
    useOfflineQueueStore.getState().remove("1");
    expect(useOfflineQueueStore.getState().items.map((i) => i.id)).toEqual(["2"]);
  });

  it("el evento storage de la otra pestaña re-hidrata esta", async () => {
    useOfflineQueueStore.setState({ items: [item("1")] });
    otraPestaniaGuarda([item("1"), item("2")]);
    await act(async () => {
      window.dispatchEvent(new StorageEvent("storage", { key: "vektor_offline_queue" }));
    });
    expect(useOfflineQueueStore.getState().items.map((i) => i.id)).toEqual(["1", "2"]);
  });

  it("adoptar deja constancia de quién y cuándo", () => {
    const { tenantId: _t, userId: _u, ...legado } = item("9");
    useOfflineQueueStore.setState({ items: [legado] });
    useOfflineQueueStore.getState().adoptar("9", { tenantId: "tenant-a", userId: "user-a" });
    const adoptada = useOfflineQueueStore.getState().items[0];
    expect(adoptada?.adoptadaPor).toBe("user-a");
    expect(adoptada?.adoptadaEn).toBeTruthy();
  });
});
