import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

import { useOfflineSubmit } from "../useOfflineSubmit";
import { useOfflineQueueStore } from "@/stores/offlineQueueStore";
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
      },
    ],
  });
}

const axiosErr = (status?: number) => ({
  isAxiosError: true,
  response: status ? { status, data: {} } : undefined,
});

beforeEach(() => {
  useOfflineQueueStore.setState({ items: [] });
  mockCreateSale.mockReset();
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
