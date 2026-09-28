/**
 * El panel que resuelve una salida de stock pendiente (B11).
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { StockDiscrepanciesPanel } from "@/features/products/StockDiscrepanciesPanel";
import { posService } from "@/services/pos.service";
import { useAuthStore } from "@/stores/authStore";

jest.mock("@/services/pos.service", () => ({
  posService: {
    listStockDiscrepancies: jest.fn(),
    resolveStockDiscrepancy: jest.fn(),
  },
}));
const svc = posService as jest.Mocked<typeof posService>;

const diferencia = {
  id: "d1",
  operation_id: "op1",
  product_id: "p1",
  product_name: "Yerba",
  pending_units: 5,
  stock_at_capture: 3,
  operation_date: "2026-09-26T10:00:00",
  status: "OPEN",
  resolution: null,
  reason: null,
  resolved_at: null,
  created_at: "2026-09-27T09:00:00",
};

function montar() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <StockDiscrepanciesPanel />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  useAuthStore.setState({
    token: "t",
    user: { id: "u1", email: "a@b.com", full_name: "Ana", role: "OWNER", tenant_id: "t1" },
  });
});

it("no se muestra si no hay diferencias abiertas", async () => {
  svc.listStockDiscrepancies.mockResolvedValue([]);
  montar();
  await waitFor(() => expect(svc.listStockDiscrepancies).toHaveBeenCalled());
  expect(screen.queryByText(/Diferencias de stock/)).not.toBeInTheDocument();
});

it("no le pregunta al servidor si el usuario no puede resolver", () => {
  // Resolver mueve stock: sin el gate, /products le pediría un 403 a cada
  // visita de un usuario de sólo lectura.
  useAuthStore.setState({
    token: "t",
    user: { id: "u2", email: "v@b.com", full_name: "Vito", role: "VIEWER", tenant_id: "t1" },
  });
  svc.listStockDiscrepancies.mockResolvedValue([diferencia]);
  montar();
  expect(svc.listStockDiscrepancies).not.toHaveBeenCalled();
});

it("muestra la salida pendiente COMPLETA y lo que el sistema tenía", async () => {
  svc.listStockDiscrepancies.mockResolvedValue([diferencia]);
  montar();
  // 5 pendientes, no un faltante de 2: las 5 salieron del negocio.
  expect(await screen.findByText(/Yerba · 5 unidades/)).toBeInTheDocument();
  expect(screen.getByText(/el sistema tenía 3/)).toBeInTheDocument();
});

it("exige un motivo y resuelve con la opción elegida", async () => {
  svc.listStockDiscrepancies.mockResolvedValue([diferencia]);
  svc.resolveStockDiscrepancy.mockResolvedValue({ ...diferencia, status: "RESOLVED" });
  montar();
  const abrir = await screen.findByRole("button", { name: "Resolver" });
  await act(async () => {
    fireEvent.click(abrir);
  });
  const confirmar = screen.getAllByRole("button", { name: "Resolver" }).at(-1)!;
  // Sin motivo no se puede: es lo único que explica después por qué se decidió.
  expect(confirmar).toBeDisabled();

  fireEvent.click(screen.getByLabelText(/La venta está mal/));
  fireEvent.change(screen.getByLabelText("Motivo"), {
    target: { value: "Se tipeó mal la cantidad." },
  });
  await act(async () => {
    fireEvent.click(screen.getAllByRole("button", { name: "Resolver" }).at(-1)!);
  });
  expect(svc.resolveStockDiscrepancy).toHaveBeenCalledWith(
    "d1",
    "anular_ticket",
    "Se tipeó mal la cantidad.",
  );
});
