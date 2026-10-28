import "@testing-library/jest-dom";
import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ForecastPanel } from "@/features/dashboard/ForecastPanel";
import { fetchCashForecast } from "@/services/dashboard.service";
import type { CashForecastResponse } from "@/types/api";

jest.mock("@/services/dashboard.service", () => ({
  fetchCashForecast: jest.fn(),
}));

const mockForecast = fetchCashForecast as jest.Mock;

function proyeccion(credito: number): CashForecastResponse {
  return {
    tier: 1,
    confidence: "LOW",
    data_days: 20,
    horizon_days: 14,
    points: [{ date: "2026-10-29", income: 1000, expense: 600, net: 400 }],
    message: null,
    credit_card_pending_ars: credito,
  };
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ForecastPanel />
    </QueryClientProvider>,
  );
}

describe("ForecastPanel — tarjeta de crédito sin acreditar (D-3/R-10)", () => {
  beforeEach(() => jest.clearAllMocks());

  test("avisa cuánto vendido con crédito no cuenta como caja", async () => {
    mockForecast.mockResolvedValue(proyeccion(10000));
    renderPanel();

    const aviso = await screen.findByTestId("forecast-credito-sin-acreditar");
    expect(aviso).toHaveTextContent(/tarjeta de crédito/i);
    expect(aviso).toHaveTextContent(/10\.000/);
  });

  test("sin ventas con crédito no muestra el aviso", async () => {
    mockForecast.mockResolvedValue(proyeccion(0));
    renderPanel();

    await waitFor(() => expect(screen.getByText(/Neto proyectado/i)).toBeInTheDocument());
    expect(screen.queryByTestId("forecast-credito-sin-acreditar")).not.toBeInTheDocument();
  });
});
