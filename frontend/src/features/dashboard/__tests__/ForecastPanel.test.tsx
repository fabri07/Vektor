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

function proyeccion(pendiente: number, dias = 14): CashForecastResponse {
  return {
    tier: 1,
    confidence: "LOW",
    data_days: 20,
    horizon_days: 14,
    points: [{ date: "2026-10-29", income: 1000, expense: 600, net: 400 }],
    message: null,
    credit_card_pending_ars: pendiente,
    credit_card_settlement_days: dias,
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

describe("ForecastPanel — tarjeta de crédito a N días (D-3/R-10)", () => {
  beforeEach(() => jest.clearAllMocks());

  test("dice a cuántos días cuenta el crédito y dónde cambiarlo", async () => {
    mockForecast.mockResolvedValue(proyeccion(0, 21));
    renderPanel();

    const aviso = await screen.findByTestId("forecast-acreditacion-credito");
    expect(aviso).toHaveTextContent(/tarjeta de crédito se cuentan a 21 días de la venta/i);
    expect(screen.getByRole("link", { name: /Margen objetivo/i })).toHaveAttribute(
      "href",
      "/settings?tab=salud",
    );
  });

  test("muestra lo vendido con crédito que todavía no se acreditó", async () => {
    mockForecast.mockResolvedValue(proyeccion(10000));
    renderPanel();

    const pendiente = await screen.findByTestId("forecast-credito-sin-acreditar");
    expect(pendiente).toHaveTextContent(/todavía no se acreditó/i);
    expect(pendiente).toHaveTextContent(/10\.000/);
    // Nunca el rótulo viejo, que presentaba 180 días de crédito como plata por cobrar.
    expect(screen.queryByText(/no sabemos cuándo/i)).not.toBeInTheDocument();
  });

  test("sin crédito pendiente no muestra el monto, pero sí el plazo", async () => {
    mockForecast.mockResolvedValue(proyeccion(0));
    renderPanel();

    await waitFor(() => expect(screen.getByText(/Neto proyectado/i)).toBeInTheDocument());
    expect(screen.queryByTestId("forecast-credito-sin-acreditar")).not.toBeInTheDocument();
    expect(screen.getByTestId("forecast-acreditacion-credito")).toHaveTextContent(/14 días/);
  });
});
