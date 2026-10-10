import "@testing-library/jest-dom";
import React from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { EconomicSummaryScreen } from "@/features/economic-summary/EconomicSummaryScreen";
import { economicSummaryService } from "@/services/economic-summary.service";
import { ownerCapitalService } from "@/services/owner-capital.service";
import { useAuthStore } from "@/stores/authStore";

jest.mock("@/services/economic-summary.service", () => ({
  economicSummaryService: { getSummary: jest.fn() },
}));
jest.mock("@/services/owner-capital.service", () => ({
  // Tope chico para poder probar el aviso de listado incompleto con 2 filas.
  CAPITAL_LIST_LIMIT: 2,
  ownerCapitalService: {
    getSummary: jest.fn(),
    list: jest.fn(),
    create: jest.fn(),
    void: jest.fn(),
  },
}));
jest.mock("@/features/dashboard/ForecastPanel", () => ({ ForecastPanel: () => null }));

const svc = ownerCapitalService as jest.Mocked<typeof ownerCapitalService>;

const cero = { dinero: 0, especie: 0 };
// Montos con decimales "raros" a propósito: si la pantalla sumara o restara,
// no aparecerían tal cual.
const SUMMARY = {
  from_date: "2026-10-01",
  to_date: "2026-10-31",
  has_data: true,
  has_in_kind: false,
  partners: [
    {
      partner_name: "Juan",
      aportes: { dinero: 100000, especie: 0 },
      retiros: { dinero: 10000, especie: 0 },
      prestamos_recibidos: cero,
      devoluciones: cero,
      prestamo_pendiente: 0,
      prestamo_pendiente_inconsistente: false,
    },
    {
      partner_name: null,
      aportes: { dinero: 700, especie: 0 },
      retiros: cero,
      prestamos_recibidos: cero,
      devoluciones: { dinero: 20000, especie: 0 },
      prestamo_pendiente: 30000,
      prestamo_pendiente_inconsistente: false,
    },
  ],
  totals: {
    partner_name: null,
    aportes: { dinero: 111111.11, especie: 0 },
    retiros: { dinero: 10000, especie: 0 },
    prestamos_recibidos: cero,
    devoluciones: { dinero: 20000, especie: 0 },
    prestamo_pendiente: 30000,
    prestamo_pendiente_inconsistente: false,
  },
  cash_flow: { entradas: 100700, salidas: 30000, neto: 12345.67 },
};

const MOVIMIENTO = {
  id: "m1",
  kind: "retiro" as const,
  form: "dinero" as const,
  amount_ars: 10000,
  occurred_on: "2026-10-05",
  payment_method: "cash",
  partner_name: "Juan",
  notes: null,
  source_type: "manual",
  created_at: "2026-10-05T12:00:00Z",
  created_by_user_id: null,
  voided_at: null,
  void_reason: null,
};

function setRole(role: string | null) {
  useAuthStore.setState({
    user: role
      ? { id: "u1", email: "a@a.com", full_name: "A", role, tenant_id: "t1" }
      : null,
  });
}

function renderScreen() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <EconomicSummaryScreen />
    </QueryClientProvider>,
  );
}

beforeAll(() => {
  if (typeof globalThis.crypto?.randomUUID !== "function") {
    Object.defineProperty(globalThis, "crypto", {
      value: { ...globalThis.crypto, randomUUID: () => "uuid-test" },
      configurable: true,
    });
  }
});

beforeEach(() => {
  jest.clearAllMocks();
  (economicSummaryService.getSummary as jest.Mock).mockResolvedValue({
    from_date: "2026-10-01",
    to_date: "2026-10-31",
    total_income_ars: 0,
    total_expenses_ars: 0,
    net_result_ars: 0,
    stock_value_ars: 0,
    missing_cost_count: 0,
    missing_cost_stock_units: 0,
    has_data: false,
  });
  svc.getSummary.mockResolvedValue(SUMMARY);
  svc.list.mockResolvedValue([MOVIMIENTO]);
  svc.create.mockResolvedValue(MOVIMIENTO);
  svc.void.mockResolvedValue({ ...MOVIMIENTO, voided_at: "2026-10-06T00:00:00Z" });
});

describe("Capital de los socios (D·4)", () => {
  test.each(["OWNER", "ADMIN"])("aparece para %s con los montos del backend", async (role) => {
    setRole(role);
    renderScreen();

    await waitFor(() => expect(screen.getByText("Capital de los socios")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText("Sin socio identificado")).toBeInTheDocument());
    expect(svc.getSummary).toHaveBeenCalledWith(
      expect.objectContaining({ from_date: expect.any(String), to_date: expect.any(String) }),
    );
    // Números tal cual: el total y el flujo vienen del backend, no de sumar filas.
    expect(screen.getByText(/111\.111,11/)).toBeInTheDocument();
    expect(screen.getByText(/12\.345,67/)).toBeInTheDocument();
    // Separación explícita del flujo operativo y de la caja contada.
    expect(screen.getByText(/No están en Ingresos, Egresos ni\s+Resultado/)).toBeInTheDocument();
    expect(screen.queryByText(/balance contable(?! oficial)/i)).not.toBeInTheDocument();
    // Sin especie, no hay líneas de especie.
    expect(screen.queryByText(/en especie:/)).not.toBeInTheDocument();
  });

  test.each(["VIEWER", "CASHIER"])("no se monta ni se consulta para %s", async (role) => {
    setRole(role);
    renderScreen();

    await waitFor(() =>
      expect(screen.getByText(/no es un balance contable oficial/i)).toBeInTheDocument(),
    );
    expect(screen.queryByText("Capital de los socios")).not.toBeInTheDocument();
    expect(svc.getSummary).not.toHaveBeenCalled();
    expect(svc.list).not.toHaveBeenCalled();
  });

  test("avisa cuando el listado llega al tope y puede haber más", async () => {
    setRole("OWNER");
    svc.list.mockResolvedValue([MOVIMIENTO, { ...MOVIMIENTO, id: "otro" }]);
    renderScreen();

    await waitFor(() =>
      expect(screen.getByText(/Se muestran los 2 más recientes; puede haber más/)).toBeInTheDocument(),
    );
  });

  test("con menos filas que el tope no hay aviso", async () => {
    setRole("OWNER");
    renderScreen();

    await waitFor(() => expect(screen.getByText("Movimientos del período")).toBeInTheDocument());
    expect(screen.queryByText(/puede haber más/)).not.toBeInTheDocument();
  });

  test("anular pide motivo y llama al endpoint", async () => {
    setRole("OWNER");
    renderScreen();

    const boton = await screen.findByRole("button", { name: /Anular Retiro del 2026-10-05/ });
    fireEvent.click(boton);
    const dialogo = await screen.findByRole("dialog");
    fireEvent.click(within(dialogo).getByRole("button", { name: "Anular" }));
    expect(await within(dialogo).findByText(/Contá por qué lo anulás/)).toBeInTheDocument();
    expect(svc.void).not.toHaveBeenCalled();

    fireEvent.change(within(dialogo).getByLabelText("Motivo"), {
      target: { value: "lo cargué dos veces" },
    });
    fireEvent.click(within(dialogo).getByRole("button", { name: "Anular" }));
    await waitFor(() => expect(svc.void).toHaveBeenCalledWith("m1", "lo cargué dos veces"));
  });

  test("el alta manda el tipo elegido", async () => {
    setRole("OWNER");
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: /Registrar movimiento de socio/ }));
    const dialogo = await screen.findByRole("dialog");
    fireEvent.click(within(dialogo).getByRole("button", { name: "Préstamo de socio" }));
    fireEvent.change(within(dialogo).getByLabelText("Monto"), { target: { value: "50000" } });
    fireEvent.change(within(dialogo).getByLabelText("Socio (opcional)"), {
      target: { value: " Ana " },
    });
    fireEvent.click(within(dialogo).getByRole("button", { name: "Registrar" }));

    await waitFor(() => expect(svc.create).toHaveBeenCalledTimes(1));
    const [payload, key] = svc.create.mock.calls[0]!;
    expect(payload).toEqual(
      expect.objectContaining({ kind: "prestamo_socio", amount: 50000, partner_name: "Ana" }),
    );
    expect(typeof key).toBe("string");
  });
});
