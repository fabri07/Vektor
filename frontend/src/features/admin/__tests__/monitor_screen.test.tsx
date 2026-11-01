import "@testing-library/jest-dom";
import React from "react";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { MonitorScreen } from "@/features/admin/MonitorScreen";
import { adminMonitorService } from "@/services/adminMonitor.service";
import type { MonitorResponse } from "@/services/adminMonitor.service";

jest.mock("@/services/adminMonitor.service", () => ({
  adminMonitorService: { get: jest.fn() },
}));

jest.mock("@/services/adminSentry.service", () => ({
  adminSentryService: {
    issues: jest.fn().mockResolvedValue({
      configured: false,
      dias: 14,
      projects: [],
      issues: [],
      total: 0,
    }),
  },
  sentryErrorMessage: () => "",
}));

const svc = adminMonitorService as jest.Mocked<typeof adminMonitorService>;

const BASE: MonitorResponse = {
  generado_en: "2026-10-31T12:00:00Z",
  negocios: { total: 7, active: 4, trial: 2, read_only: 1, suspended: 0 },
  suscripciones: {
    trial: 2,
    active: 4,
    grace: 1,
    read_only: 0,
    sin_suscripcion: 0,
    vencen_en_7_dias: 3,
  },
  actividad: { hoy: 2, ultimos_7_dias: 5, ultimos_30_dias: 6 },
  ia: {
    dias: 30,
    tokens_total: 0,
    cost_usd: 0,
    unpriced_tokens: 0,
    ultimo_consumo: "2026-07-17",
  },
  atencion: [
    {
      tenant_id: "t-1",
      display_name: "Kiosco Don Pedro",
      motivo: "GRACE",
      fecha: "2026-11-02T00:00:00Z",
    },
    { tenant_id: "t-2", display_name: "Limpieza Sur", motivo: "INACTIVE", fecha: null },
  ],
  atencion_total: 2,
};

function conCliente(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("MonitorScreen", () => {
  it("muestra los conteos y lleva cada negocio de atención a su ficha", async () => {
    svc.get.mockResolvedValue(BASE);
    conCliente(<MonitorScreen />);
    expect(await screen.findByText("Negocios (sin demos)")).toBeInTheDocument();
    expect(screen.getByText("Vencen en 7 días")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: /Kiosco Don Pedro/ });
    expect(link).toHaveAttribute("href", "/admin/tenants/t-1");
    expect(link).toHaveTextContent("En período de gracia");
    // Sin fecha de último uso se dice «nunca», no un guion ni una fecha inventada.
    expect(screen.getByRole("link", { name: /Limpieza Sur/ })).toHaveTextContent("nunca");
  });

  it("con IA en cero dice cuándo fue el último consumo, sin inventar costo", async () => {
    svc.get.mockResolvedValue(BASE);
    conCliente(<MonitorScreen />);
    expect(await screen.findByText("17/07/2026")).toBeInTheDocument();
    expect(screen.getByText("$0.00")).toBeInTheDocument();
  });

  it("nunca hubo consumo: lo dice", async () => {
    svc.get.mockResolvedValue({ ...BASE, ia: { ...BASE.ia, ultimo_consumo: null } });
    conCliente(<MonitorScreen />);
    expect(await screen.findByText("Nunca")).toBeInTheDocument();
  });

  it("sin negocios para atender muestra el estado vacío", async () => {
    svc.get.mockResolvedValue({ ...BASE, atencion: [], atencion_total: 0 });
    conCliente(<MonitorScreen />);
    expect(
      await screen.findByText("Ningún negocio necesita atención ahora."),
    ).toBeInTheDocument();
  });

  it("avisa cuando hay negocios sin suscripción y cuando la lista está recortada", async () => {
    svc.get.mockResolvedValue({
      ...BASE,
      suscripciones: { ...BASE.suscripciones, sin_suscripcion: 2 },
      atencion_total: 30,
    });
    conCliente(<MonitorScreen />);
    expect(await screen.findByText(/recibe error 503/)).toBeInTheDocument();
    expect(screen.getByText(/Se muestran 2 de 30/)).toBeInTheDocument();
  });

  it("si falla la consulta, lo dice", async () => {
    svc.get.mockRejectedValue(new Error("x"));
    conCliente(<MonitorScreen />);
    expect(await screen.findByText("No se pudo cargar el monitor")).toBeInTheDocument();
  });
});
