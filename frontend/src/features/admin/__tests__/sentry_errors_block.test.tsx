import "@testing-library/jest-dom";
import React from "react";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { SentryErrorsBlock } from "@/features/admin/SentryErrorsBlock";
import { adminSentryService, sentryErrorMessage } from "@/services/adminSentry.service";
import type { SentryIssuesResponse } from "@/services/adminSentry.service";

jest.mock("@/services/adminSentry.service", () => {
  const actual = jest.requireActual("@/services/adminSentry.service");
  return { ...actual, adminSentryService: { issues: jest.fn() } };
});

const svc = adminSentryService as jest.Mocked<typeof adminSentryService>;

const BASE: SentryIssuesResponse = {
  configured: true,
  dias: 14,
  projects: ["python-fastapi"],
  total: 1,
  issues: [
    {
      id: "1",
      short_id: "PYTHON-FASTAPI-B",
      title: "Resend rechazó el destinatario",
      project: "python-fastapi",
      service: "worker",
      level: "error",
      events: 4,
      first_seen: "2026-10-30T12:00:00Z",
      last_seen: "2026-10-31T12:00:00Z",
      url: "https://vektor-g9.sentry.io/issues/1/",
    },
  ],
};

function conCliente(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("SentryErrorsBlock", () => {
  it("lista los errores con enlace a Sentry que abre en otra pestaña", async () => {
    svc.issues.mockResolvedValue(BASE);
    conCliente(<SentryErrorsBlock />);
    const link = await screen.findByRole("link", { name: /Resend rechazó/ });
    expect(link).toHaveAttribute("href", "https://vektor-g9.sentry.io/issues/1/");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", expect.stringContaining("noopener"));
    expect(link).toHaveTextContent("PYTHON-FASTAPI-B · python-fastapi · worker · 4 eventos");
  });

  it("no configurado NO se muestra como «sin errores»", async () => {
    svc.issues.mockResolvedValue({ ...BASE, configured: false, issues: [], total: 0 });
    conCliente(<SentryErrorsBlock />);
    expect(await screen.findByText(/Sentry no está configurado/)).toBeInTheDocument();
    expect(screen.queryByText(/Sin errores sin resolver/)).not.toBeInTheDocument();
  });

  it("configurado y sin issues dice que no hay errores en la ventana", async () => {
    svc.issues.mockResolvedValue({ ...BASE, issues: [], total: 0 });
    conCliente(<SentryErrorsBlock />);
    expect(
      await screen.findByText("Sin errores sin resolver en los últimos 14 días."),
    ).toBeInTheDocument();
  });

  it("avisa cuando la lista está recortada", async () => {
    svc.issues.mockResolvedValue({ ...BASE, total: 40 });
    conCliente(<SentryErrorsBlock />);
    expect(await screen.findByText("Se muestran 1 de 40.")).toBeInTheDocument();
  });

  it("si Sentry falla muestra el motivo por código, sin romper", async () => {
    svc.issues.mockRejectedValue({
      response: { data: { detail: { code: "SENTRY_TOKEN_REJECTED" } } },
    });
    conCliente(<SentryErrorsBlock />);
    expect(await screen.findByText(/Sentry rechazó el token/)).toBeInTheDocument();
  });
});

describe("sentryErrorMessage", () => {
  it("cae a un genérico con un error desconocido", () => {
    expect(sentryErrorMessage(new Error("x"))).toBe(
      "No se pudieron cargar los errores de Sentry.",
    );
  });
});
