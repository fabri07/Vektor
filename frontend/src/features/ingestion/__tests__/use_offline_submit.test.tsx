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
  it("removes a permanent 4xx item (no infinite retry)", async () => {
    seed();
    mockCreateSale.mockRejectedValue(axiosErr(400));
    const { result } = renderHook(() => useOfflineSubmit(), { wrapper });
    await act(async () => {
      await result.current.flush();
    });
    expect(useOfflineQueueStore.getState().items).toHaveLength(0);
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
    expect(items[0].attempts).toBe(1);
  });

  it("drops a 5xx item once it hits the attempt cap", async () => {
    seed(4); // next attempt = 5 = MAX_FLUSH_ATTEMPTS
    mockCreateSale.mockRejectedValue(axiosErr(503));
    const { result } = renderHook(() => useOfflineSubmit(), { wrapper });
    await act(async () => {
      await result.current.flush();
    });
    expect(useOfflineQueueStore.getState().items).toHaveLength(0);
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
