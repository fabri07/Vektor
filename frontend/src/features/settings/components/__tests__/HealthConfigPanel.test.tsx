/**
 * D-3/R-10: el plazo de acreditación de la tarjeta de crédito se ve y se cambia
 * en Configuración → Score de salud, y guardarlo manda SÓLO ese campo (no pisa
 * los márgenes ni los marca como personalizados).
 */
import "@testing-library/jest-dom";
import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { HealthConfigPanel } from "../HealthConfigPanel";
import { settingsService, type HealthConfig } from "@/services/settings.service";
import { useToastStore } from "@/stores/toastStore";

jest.mock("@/services/settings.service", () => ({
  settingsService: {
    getHealthConfig: jest.fn(),
    updateHealthConfig: jest.fn(),
    updateCreditCardSettlementDays: jest.fn(),
    resetHealthConfig: jest.fn(),
  },
}));

const svc = settingsService as jest.Mocked<typeof settingsService>;

function config(over: Partial<HealthConfig> = {}): HealthConfig {
  return {
    target_margin_pct: 35,
    warning_margin_pct: 20,
    is_custom: false,
    credit_card_settlement_days: 14,
    credit_card_settlement_days_is_custom: false,
    margin_kind: "bruto",
    ...over,
  };
}

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <HealthConfigPanel />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  useToastStore.setState({ toasts: [] });
});

test("muestra el plazo guardado", async () => {
  svc.getHealthConfig.mockResolvedValue(config({ credit_card_settlement_days: 30 }));
  renderPanel();

  const input = await screen.findByLabelText(/Días hasta la acreditación/i);
  await waitFor(() => expect(input).toHaveValue(30));
  expect(screen.getByText(/Tarjeta de crédito en la proyección de caja/i)).toBeInTheDocument();
});

test("guardar el plazo manda sólo N", async () => {
  svc.getHealthConfig.mockResolvedValue(config());
  svc.updateCreditCardSettlementDays.mockResolvedValue(
    config({ credit_card_settlement_days: 0, credit_card_settlement_days_is_custom: true }),
  );
  renderPanel();

  const input = await screen.findByLabelText(/Días hasta la acreditación/i);
  await waitFor(() => expect(input).toHaveValue(14));
  fireEvent.change(input, { target: { value: "0" } });
  fireEvent.click(screen.getByRole("button", { name: /Guardar plazo/i }));

  await waitFor(() => expect(svc.updateCreditCardSettlementDays).toHaveBeenCalledWith(0));
  expect(svc.updateHealthConfig).not.toHaveBeenCalled();
});

test("fuera de rango no deja guardar", async () => {
  svc.getHealthConfig.mockResolvedValue(config());
  renderPanel();

  const input = await screen.findByLabelText(/Días hasta la acreditación/i);
  fireEvent.change(input, { target: { value: "61" } });

  expect(screen.getByRole("button", { name: /Guardar plazo/i })).toBeDisabled();
  expect(screen.getByText(/entero de 0 a 60/i)).toBeInTheDocument();
});

test("con sólo el plazo personalizado se puede restablecer", async () => {
  svc.getHealthConfig.mockResolvedValue(
    config({ credit_card_settlement_days: 30, credit_card_settlement_days_is_custom: true }),
  );
  renderPanel();

  expect(
    await screen.findByRole("button", { name: /Restablecer predeterminados/i }),
  ).toBeInTheDocument();
});
