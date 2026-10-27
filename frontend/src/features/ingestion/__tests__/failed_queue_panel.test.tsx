import { act, render as rtlRender, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";

import * as Sentry from "@sentry/nextjs";
import { FailedQueuePanel, resumenDeCarga } from "../FailedQueuePanel";
import { useOfflineQueueStore, type QueuedItem } from "@/stores/offlineQueueStore";
import { salesService } from "@/services/sales.service";
import { useAuthStore } from "@/stores/authStore";

jest.mock("@sentry/nextjs", () => ({
  addBreadcrumb: jest.fn(),
  setUser: jest.fn(),
  setTag: jest.fn(),
}));

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
        tenantId: "tenant-a",
        userId: "user-a",
        ...over,
      },
    ],
  });
}

function entrarComo(tenantId: string, userId: string, role = "OWNER") {
  useAuthStore.setState({
    user: { id: userId, email: `${userId}@x.com`, full_name: userId, role, tenant_id: tenantId },
  });
}

beforeEach(() => {
  useOfflineQueueStore.setState({ items: [] });
  entrarComo("tenant-a", "user-a");
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
          tenantId: "tenant-a",
          userId: "user-a",
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

  it("X6-1: la rechazada del negocio A no se le muestra a la sesión del negocio B", () => {
    seedFailed();
    entrarComo("tenant-b", "user-b");
    render(<FailedQueuePanel />);
    // Sólo el aviso sin contenido; nada de la carga ni sus botones.
    expect(screen.queryByText(/que el servidor rechazó/)).not.toBeInTheDocument();
    expect(screen.queryByText("Error 400 del servidor")).not.toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  describe("cargas viejas sin dueño (X6-1)", () => {
    const SIN_DUENIO = { tenantId: undefined, userId: undefined, status: undefined, attempts: 0 };

    it("se muestran aparte y NO se mandan solas", () => {
      seedFailed(SIN_DUENIO);
      render(<FailedQueuePanel />);
      expect(screen.getByText(/sin usuario asignado/)).toBeInTheDocument();
      expect(screen.queryByText(/que el servidor rechazó/)).not.toBeInTheDocument();
      expect(mockCreateManualBatch).not.toHaveBeenCalled();
    });

    it("«Es mía, subirla» pide confirmación, la adopta y la manda", async () => {
      seedFailed(SIN_DUENIO);
      mockCreateManualBatch.mockResolvedValue({ sale_group_id: "g", sales: [], total: 0 });
      const confirmSpy = jest.spyOn(window, "confirm").mockReturnValue(false);
      render(<FailedQueuePanel />);
      await act(async () => {
        await userEvent.click(screen.getByRole("button", { name: "Es mía, subirla" }));
      });
      expect(mockCreateManualBatch).not.toHaveBeenCalled();
      expect(useOfflineQueueStore.getState().items[0]?.tenantId).toBeUndefined();

      confirmSpy.mockReturnValue(true);
      await act(async () => {
        await userEvent.click(screen.getByRole("button", { name: "Es mía, subirla" }));
      });
      expect(mockCreateManualBatch).toHaveBeenCalledWith({}, "22222222-2222-2222-2222-222222222222");
      expect(useOfflineQueueStore.getState().items).toHaveLength(0);
      confirmSpy.mockRestore();
    });

    it("el confirm muestra un resumen reconocible y queda un breadcrumb sin payload", async () => {
      seedFailed({
        ...SIN_DUENIO,
        kind: "sale",
        payload: { amount: 1500, transaction_date: "2026-06-15", notes: "Fiambre Juan" },
      });
      const confirmSpy = jest.spyOn(window, "confirm").mockReturnValue(true);
      render(<FailedQueuePanel />);
      await act(async () => {
        await userEvent.click(screen.getByRole("button", { name: "Es mía, subirla" }));
      });
      const mensaje = confirmSpy.mock.calls[0]?.[0] ?? "";
      expect(mensaje).toContain("Venta");
      expect(mensaje).toContain("2026-06-15");
      expect(mensaje).toContain("1.500");
      expect(mensaje).toContain("Fiambre Juan");
      const crumb = (Sentry.addBreadcrumb as jest.Mock).mock.calls.at(-1)?.[0];
      expect(crumb.data).toEqual({ id: "22222222-2222-2222-2222-222222222222", kind: "sale" });
      expect(JSON.stringify(crumb)).not.toContain("Fiambre");
      confirmSpy.mockRestore();
    });

    it.each(["CASHIER", "VIEWER", "ANALYST"])("%s ve la carga pero no puede reclamarla ni descartarla", (rol) => {
      seedFailed(SIN_DUENIO);
      entrarComo("tenant-a", "user-a", rol);
      render(<FailedQueuePanel />);
      expect(screen.getByText(/sin usuario asignado/)).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Es mía, subirla" })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Descartar" })).not.toBeInTheDocument();
    });

    it("Descartar sólo borra si el usuario confirma", async () => {
      seedFailed(SIN_DUENIO);
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

  it("avisa cuántas cargas de otro usuario hay en la PC, sin mostrar su contenido", () => {
    seedFailed({ lastError: "Error de Juan", tenantId: "tenant-b", userId: "user-b" });
    render(<FailedQueuePanel />);
    expect(screen.getByText(/Hay 1 carga\(s\) de otro usuario/)).toBeInTheDocument();
    expect(screen.queryByText("Error de Juan")).not.toBeInTheDocument();
  });

  it("resumenDeCarga arma tipo, fecha, monto, líneas y descripción", () => {
    expect(
      resumenDeCarga({
        id: "x",
        kind: "purchase",
        payload: { transaction_date: "2026-06-01", lines: [{}, {}] },
        createdAt: "2026-06-01T10:00:00Z",
        attempts: 0,
      }),
    ).toBe("Compra · 2026-06-01 · 2 línea(s)");
  });
});
