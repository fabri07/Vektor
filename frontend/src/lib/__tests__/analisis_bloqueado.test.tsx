import "@testing-library/jest-dom";
import React from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AxiosError, AxiosHeaders } from "axios";

import { ForecastPanel } from "@/features/dashboard/ForecastPanel";
import { EconomicSummaryScreen } from "@/features/economic-summary/EconomicSummaryScreen";
import { queryClient as appQueryClient } from "@/lib/queryClient";
import { esAnalisisBloqueado, reintentarSalvoBloqueo } from "@/lib/subscription";
import { fetchCashForecast } from "@/services/dashboard.service";
import { economicSummaryService } from "@/services/economic-summary.service";

jest.mock("@/services/economic-summary.service", () => ({
  economicSummaryService: { getSummary: jest.fn() },
}));
jest.mock("@/services/dashboard.service", () => ({
  fetchCashForecast: jest.fn(),
}));

const mockGetSummary = economicSummaryService.getSummary as jest.Mock;
const mockForecast = fetchCashForecast as jest.Mock;

function errorHttp(status: number, data: unknown): AxiosError {
  const headers = new AxiosHeaders();
  return new AxiosError("error", "ERR_BAD_RESPONSE", { headers }, undefined, {
    status,
    statusText: "",
    headers: {},
    config: { headers },
    data,
  });
}

const BLOQUEADO = errorHttp(402, {
  detail: { code: "SUBSCRIPTION_INSIGHTS_LOCKED", reason: "solo_lectura" },
});

const RESUMEN = {
  from_date: "2026-09-01",
  to_date: "2026-09-29",
  total_income_ars: 15000,
  total_expenses_ars: 3000,
  net_result_ars: 12000,
  stock_value_ars: 2000,
  missing_cost_count: 0,
  missing_cost_stock_units: 0,
  has_data: true,
};

/** Un cliente con la MISMA política de reintento que la app, sin espera entre intentos. */
function clienteComoLaApp(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: appQueryClient.getDefaultOptions().queries?.retry, retryDelay: 0 },
    },
  });
}

function renderCon(client: QueryClient, ui: React.ReactElement) {
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

describe("esAnalisisBloqueado", () => {
  test("reconoce el 402 de análisis en sus dos formas", () => {
    expect(esAnalisisBloqueado(BLOQUEADO)).toBe(true);
    expect(esAnalisisBloqueado(errorHttp(402, { code: "SUBSCRIPTION_INSIGHTS_LOCKED" }))).toBe(true);
  });

  test("no confunde el bloqueo de escritura ni otros errores", () => {
    expect(esAnalisisBloqueado(errorHttp(402, { detail: { code: "SUBSCRIPTION_READ_ONLY" } }))).toBe(
      false,
    );
    expect(esAnalisisBloqueado(errorHttp(500, {}))).toBe(false);
    expect(esAnalisisBloqueado(new Error("red"))).toBe(false);
    expect(esAnalisisBloqueado(undefined)).toBe(false);
  });

  test("un 402 no se reintenta; otro error, una vez", () => {
    expect(reintentarSalvoBloqueo(0, BLOQUEADO)).toBe(false);
    expect(reintentarSalvoBloqueo(0, errorHttp(500, {}))).toBe(true);
    expect(reintentarSalvoBloqueo(1, errorHttp(500, {}))).toBe(false);
  });
});

describe("pantallas de análisis con la suscripción bloqueada", () => {
  beforeEach(() => jest.clearAllMocks());

  test("el balance muestra el aviso y NO reintenta", async () => {
    mockGetSummary.mockRejectedValue(BLOQUEADO);
    renderCon(clienteComoLaApp(), <EconomicSummaryScreen />);

    await waitFor(() => expect(screen.getByTestId("analisis-bloqueado")).toBeInTheDocument());
    expect(screen.getByText(/la caja sigue cobrando/i)).toBeInTheDocument();
    expect(screen.queryByText(/Error al cargar el resumen/i)).not.toBeInTheDocument();
    expect(mockGetSummary).toHaveBeenCalledTimes(1);
  });

  test("un error común sí se reintenta y se muestra como error", async () => {
    mockGetSummary.mockRejectedValue(errorHttp(500, {}));
    renderCon(clienteComoLaApp(), <EconomicSummaryScreen />);

    await waitFor(() =>
      expect(screen.getByText(/Error al cargar el resumen/i)).toBeInTheDocument(),
    );
    expect(mockGetSummary).toHaveBeenCalledTimes(2);
    expect(screen.queryByTestId("analisis-bloqueado")).not.toBeInTheDocument();
  });

  test("al regularizar y volver a pedir los datos, el aviso desaparece", async () => {
    const client = clienteComoLaApp();
    mockGetSummary.mockRejectedValue(BLOQUEADO);
    renderCon(client, <EconomicSummaryScreen />);
    await waitFor(() => expect(screen.getByTestId("analisis-bloqueado")).toBeInTheDocument());

    mockGetSummary.mockResolvedValue(RESUMEN);
    await act(async () => {
      await client.invalidateQueries();
    });

    await waitFor(() => expect(screen.getByText("Ingresos")).toBeInTheDocument());
    expect(screen.queryByTestId("analisis-bloqueado")).not.toBeInTheDocument();
  });

  test("al cambiar de cuenta (caché vaciada) el aviso no queda pegado", async () => {
    const client = clienteComoLaApp();
    mockGetSummary.mockRejectedValue(BLOQUEADO);
    const vista = renderCon(client, <EconomicSummaryScreen />);
    await waitFor(() => expect(screen.getByTestId("analisis-bloqueado")).toBeInTheDocument());

    // Lo que hace `useSessionSync` al detectar otro usuario: vaciar la caché.
    mockGetSummary.mockResolvedValue(RESUMEN);
    act(() => client.clear());
    vista.unmount();
    renderCon(client, <EconomicSummaryScreen />);

    await waitFor(() => expect(screen.getByText("Ingresos")).toBeInTheDocument());
    expect(screen.queryByTestId("analisis-bloqueado")).not.toBeInTheDocument();
  });

  test("la proyección de caja muestra el aviso compacto", async () => {
    mockForecast.mockRejectedValue(BLOQUEADO);
    renderCon(clienteComoLaApp(), <ForecastPanel />);

    await waitFor(() => expect(screen.getByTestId("analisis-bloqueado")).toBeInTheDocument());
    expect(screen.queryByText(/Sin datos suficientes para proyectar/i)).not.toBeInTheDocument();
    expect(mockForecast).toHaveBeenCalledTimes(1);
  });
});
