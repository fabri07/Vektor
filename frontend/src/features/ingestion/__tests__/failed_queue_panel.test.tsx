import { act, render as rtlRender, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";

import { FailedQueuePanel } from "../FailedQueuePanel";
import { useOfflineQueueStore, type QueuedItem } from "@/stores/offlineQueueStore";
import { salesService } from "@/services/sales.service";

jest.mock("@/services/sales.service", () => ({
  salesService: { createSale: jest.fn(), createManualBatch: jest.fn() },
}));

const mockCreateManualBatch = salesService.createManualBatch as jest.Mock;

function render(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return rtlRender(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function seedFailed(over: Partial<QueuedItem> = {}) {
  useOfflineQueueStore.setState({
    items: [
      {
        id: "22222222-2222-2222-2222-222222222222",
        kind: "sale_batch",
        payload: {},
        createdAt: "2026-09-21T14:00:00Z",
        attempts: 5,
        lastError: "Error 400 del servidor",
        status: "FAILED",
        ...over,
      },
    ],
  });
}

beforeEach(() => {
  useOfflineQueueStore.setState({ items: [] });
  // Por defecto el reintento vuelve a fallar con un error de red: los tests que
  // miran el estado de la cola no quieren que el flush la vacíe.
  mockCreateManualBatch.mockReset();
  mockCreateManualBatch.mockRejectedValue(Object.assign(new Error("offline"), { isAxiosError: true }));
});

describe("FailedQueuePanel", () => {
  it("no renderiza nada si no hay cargas rechazadas", () => {
    useOfflineQueueStore.setState({
      items: [
        {
          id: "a",
          kind: "sale",
          payload: {},
          createdAt: "2026-09-21T14:00:00Z",
          attempts: 1,
        },
      ],
    });
    const { container } = render(<FailedQueuePanel />);
    expect(container).toBeEmptyDOMElement();
  });

  it("lista la carga rechazada con su error", () => {
    seedFailed();
    render(<FailedQueuePanel />);
    expect(screen.getByText(/1 carga\(s\) que el servidor rechazó/)).toBeInTheDocument();
    expect(screen.getByText("Venta multi-producto")).toBeInTheDocument();
    expect(screen.getByText("Error 400 del servidor")).toBeInTheDocument();
  });

  it("Reintentar la devuelve a pendiente, sin borrarla", async () => {
    seedFailed();
    render(<FailedQueuePanel />);
    await act(async () => {
      await userEvent.click(screen.getByRole("button", { name: "Reintentar" }));
    });
    const items = useOfflineQueueStore.getState().items;
    expect(items).toHaveLength(1);
    expect(items[0]?.status).toBe("PENDING");
    expect(items[0]?.attempts).toBe(0);
  });

  it("Reintentar vuelve a MANDAR la carga, no sólo cambia su estado", async () => {
    seedFailed();
    mockCreateManualBatch.mockResolvedValue({ sale_group_id: "g", sales: [], total: 0 });
    render(<FailedQueuePanel />);
    await act(async () => {
      await userEvent.click(screen.getByRole("button", { name: "Reintentar" }));
    });
    expect(mockCreateManualBatch).toHaveBeenCalledWith({}, "22222222-2222-2222-2222-222222222222");
    // Se mandó y el servidor la aceptó: sale de la cola.
    expect(useOfflineQueueStore.getState().items).toHaveLength(0);
  });

  it("Descartar pide confirmación y solo borra si el usuario confirma", async () => {
    seedFailed();
    const confirmSpy = jest.spyOn(window, "confirm").mockReturnValue(false);
    render(<FailedQueuePanel />);
    await act(async () => {
      await userEvent.click(screen.getByRole("button", { name: "Descartar" }));
    });
    expect(useOfflineQueueStore.getState().items).toHaveLength(1);

    confirmSpy.mockReturnValue(true);
    await act(async () => {
      await userEvent.click(screen.getByRole("button", { name: "Descartar" }));
    });
    expect(useOfflineQueueStore.getState().items).toHaveLength(0);
    confirmSpy.mockRestore();
  });
});
