import "@testing-library/jest-dom";
import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import EconomicSummaryPage from "@/app/(protected)/resumen-economico/page";
import { economicSummaryService } from "@/services/economic-summary.service";

jest.mock("@/services/economic-summary.service", () => ({
  economicSummaryService: { getSummary: jest.fn() },
}));

const mockGetSummary = economicSummaryService.getSummary as jest.Mock;

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <EconomicSummaryPage />
    </QueryClientProvider>,
  );
}

describe("EconomicSummaryPage — FASE 4", () => {
  beforeEach(() => jest.clearAllMocks());

  test("renderiza las métricas y el disclaimer legal", async () => {
    mockGetSummary.mockResolvedValue({
      from_date: "2026-01-01",
      to_date: "2026-01-31",
      total_income_ars: 15000,
      total_expenses_ars: 3000,
      net_result_ars: 12000,
      stock_value_ars: 2000,
      missing_cost_count: 0,
      missing_cost_stock_units: 0,
      has_data: true,
    });

    renderPage();

    await waitFor(() => expect(screen.getByText("Ingresos")).toBeInTheDocument());
    expect(screen.getByText("Egresos")).toBeInTheDocument();
    expect(screen.getByText("Resultado")).toBeInTheDocument();
    expect(screen.getByText("Stock valorizado")).toBeInTheDocument();
    // Disclaimer legal siempre visible.
    expect(
      screen.getByText(/no es un balance contable oficial/i),
    ).toBeInTheDocument();
  });

  test("muestra empty state cuando no hay datos", async () => {
    mockGetSummary.mockResolvedValue({
      from_date: "2026-01-01",
      to_date: "2026-01-31",
      total_income_ars: 0,
      total_expenses_ars: 0,
      net_result_ars: 0,
      stock_value_ars: 0,
      missing_cost_count: 0,
      missing_cost_stock_units: 0,
      has_data: false,
    });

    renderPage();

    await waitFor(() =>
      expect(screen.getByText(/Sin datos en este período/i)).toBeInTheDocument(),
    );
    // El disclaimer legal aparece incluso en empty state.
    expect(
      screen.getByText(/no es un balance contable oficial/i),
    ).toBeInTheDocument();
  });

  test("avisa de productos sin costo", async () => {
    mockGetSummary.mockResolvedValue({
      from_date: "2026-01-01",
      to_date: "2026-01-31",
      total_income_ars: 0,
      total_expenses_ars: 0,
      net_result_ars: 0,
      stock_value_ars: 500,
      missing_cost_count: 2,
      missing_cost_stock_units: 8,
      has_data: true,
    });

    renderPage();

    await waitFor(() =>
      expect(screen.getByText(/2 producto\(s\) sin costo cargado/i)).toBeInTheDocument(),
    );
  });
});
