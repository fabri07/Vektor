import "@testing-library/jest-dom";
import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { TenantDetailScreen } from "@/features/admin/TenantDetailScreen";
import { TenantsScreen } from "@/features/admin/TenantsScreen";
import { isSuperadmin } from "@/lib/roles";
import { diaCorto } from "@/features/admin/format";
import { adminErrorMessage, adminTenantsService } from "@/services/adminTenants.service";
import type { AdminTenantDetail, AdminTenantListItem } from "@/services/adminTenants.service";

jest.mock("@/services/adminTenants.service", () => {
  const actual = jest.requireActual("@/services/adminTenants.service");
  return {
    ...actual,
    adminTenantsService: {
      list: jest.fn(),
      get: jest.fn(),
      changeStatus: jest.fn(),
      applyPayment: jest.fn(),
      changePlan: jest.fn(),
      cancelRenewal: jest.fn(),
      listPayments: jest.fn(),
    },
  };
});

const svc = adminTenantsService as jest.Mocked<typeof adminTenantsService>;

function conCliente(ui: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

const ITEM: AdminTenantListItem = {
  tenant_id: "t-1",
  display_name: "Kiosco Don Pedro",
  legal_name: "Pedro SA",
  vertical_code: "kiosco_almacen",
  account_status: "SUSPENDED",
  status_changed_at: null,
  is_demo: false,
  created_at: "2026-01-01T00:00:00Z",
  subscription_plan: "control",
  subscription_status: "ACTIVE",
  current_period_end: "2026-12-01T00:00:00Z",
  users_count: 3,
  last_login_at: null,
  tokens_30d: 1200,
  last_activity_date: "2026-10-29",
  active_days_30d: 12,
};

const DETALLE: AdminTenantDetail = {
  tenant_id: "t-1",
  display_name: "Kiosco Don Pedro",
  legal_name: "Pedro SA",
  vertical_code: "kiosco_almacen",
  account_status: "ACTIVE",
  status_changed_at: null,
  is_demo: false,
  created_at: "2026-01-01T00:00:00Z",
  last_activity_date: "2026-10-29",
  active_days_30d: 12,
  subscription: {
    plan_code: "control",
    status: "ACTIVE",
    current_period_start: null,
    current_period_end: "2026-12-01T00:00:00Z",
    trial_ends_at: null,
    grace_ends_at: null,
    cancel_at_period_end: false,
    seats_included: 3,
    granted_ia_queries_per_month: 70,
    granted_imports_per_month: 17,
    granted_photo_pdf_reads_per_month: 7,
  },
  users: [
    {
      user_id: "u-1",
      email: "pedro@kiosco.com",
      full_name: "Pedro",
      role_code: "OWNER",
      is_active: true,
      last_login_at: null,
    },
  ],
  recent_events: [],
};

beforeEach(() => {
  jest.clearAllMocks();
  svc.listPayments.mockResolvedValue([]);
});

describe("roles", () => {
  it("isSuperadmin sólo reconoce SUPERADMIN", () => {
    expect(isSuperadmin("SUPERADMIN")).toBe(true);
    expect(isSuperadmin("OWNER")).toBe(false);
    expect(isSuperadmin(undefined)).toBe(false);
  });
});

describe("adminErrorMessage", () => {
  it("usa el mensaje estructurado del servidor", () => {
    const e = { response: { data: { detail: { code: "USE_RENEW", message: "Corresponde renovar." } } } };
    expect(adminErrorMessage(e)).toBe("Corresponde renovar.");
  });
  it("cae a un mensaje genérico", () => {
    expect(adminErrorMessage(new Error("x"))).toMatch(/No se pudo completar/);
  });
});

describe("diaCorto", () => {
  it("muestra el día tal cual, sin correrlo por la zona horaria", () => {
    expect(diaCorto("2026-10-29")).toBe("29/10/2026");
    expect(diaCorto("2026-01-01")).toBe("01/01/2026");
  });
  it("sin día o con un formato raro muestra un guion", () => {
    expect(diaCorto(null)).toBe("—");
    expect(diaCorto("2026-10-29T00:00:00Z")).toBe("—");
  });
});

describe("TenantsScreen", () => {
  it("muestra el estado de cuenta y el de suscripción por separado", async () => {
    svc.list.mockResolvedValue({ items: [ITEM], total: 1, page: 1, page_size: 25 });
    conCliente(<TenantsScreen />);
    expect(await screen.findByText("Kiosco Don Pedro")).toBeInTheDocument();
    expect(screen.getByText("Suspendida")).toBeInTheDocument(); // cuenta
    expect(screen.getByText("Activa")).toBeInTheDocument(); // suscripción
    expect(screen.getByRole("link", { name: /Kiosco Don Pedro/ })).toHaveAttribute(
      "href",
      "/admin/tenants/t-1",
    );
  });

  it("muestra el último uso y los días con uso, separados del último ingreso", async () => {
    svc.list.mockResolvedValue({ items: [ITEM], total: 1, page: 1, page_size: 25 });
    conCliente(<TenantsScreen />);
    expect(await screen.findByText("Kiosco Don Pedro")).toBeInTheDocument();
    expect(screen.getByText("29/10/2026")).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
  });

  it("al filtrar vuelve a la primera página y manda el filtro", async () => {
    svc.list.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 25 });
    conCliente(<TenantsScreen />);
    fireEvent.change(await screen.findByLabelText("Buscar negocio"), {
      target: { value: "pedro" },
    });
    await waitFor(() =>
      expect(svc.list).toHaveBeenLastCalledWith(expect.objectContaining({ q: "pedro", page: 1 })),
    );
  });
});

describe("TenantDetailScreen", () => {
  it("no deja cambiar el estado sin motivo y manda el cambio con él", async () => {
    svc.get.mockResolvedValue(DETALLE);
    svc.changeStatus.mockResolvedValue({
      tenant_id: "t-1",
      previous_status: "ACTIVE",
      status: "READ_ONLY",
      changed: true,
      status_changed_at: "2026-10-29T10:00:00Z",
    });
    conCliente(<TenantDetailScreen tenantId="t-1" />);

    const select = await screen.findByLabelText("Cambiar estado de cuenta");
    fireEvent.change(select, { target: { value: "READ_ONLY" } });
    // La consecuencia se muestra ANTES de confirmar.
    expect(screen.getByText(/no cargar ni modificar nada/i)).toBeInTheDocument();

    const boton = screen.getByRole("button", { name: "Cambiar estado" });
    expect(boton).toBeDisabled(); // sin motivo
    fireEvent.change(screen.getByLabelText("Motivo del cambio de estado"), {
      target: { value: "falta de pago" },
    });
    expect(boton).toBeEnabled();
    fireEvent.change(screen.getByLabelText(/Tu contraseña/), { target: { value: "Secure789" } });
    fireEvent.click(boton);

    // La contraseña viaja como tercer argumento (header en el servicio).
    await waitFor(() =>
      expect(svc.changeStatus).toHaveBeenCalledWith(
        "t-1",
        { status: "READ_ONLY", reason: "falta de pago" },
        "Secure789",
      ),
    );
  });

  it("suspender usa un botón propio y no ofrece el estado actual", async () => {
    svc.get.mockResolvedValue(DETALLE);
    conCliente(<TenantDetailScreen tenantId="t-1" />);
    const select = await screen.findByLabelText("Cambiar estado de cuenta");
    expect(screen.queryByRole("option", { name: "Activa" })).not.toBeInTheDocument();
    fireEvent.change(select, { target: { value: "SUSPENDED" } });
    expect(screen.getByRole("button", { name: "Suspender cuenta" })).toBeInTheDocument();
  });

  it("muestra el error estructurado cuando el servidor rechaza un pago", async () => {
    svc.get.mockResolvedValue(DETALLE);
    svc.applyPayment.mockRejectedValue({
      response: { data: { detail: { code: "USE_RENEW", message: "Corresponde renovar." } } },
    });
    conCliente(<TenantDetailScreen tenantId="t-1" />);

    fireEvent.change(await screen.findByLabelText("Referencia del pago"), {
      target: { value: "TRF-9" },
    });
    fireEvent.change(screen.getByLabelText("Importe en ARS"), { target: { value: "40500" } });
    fireEvent.change(screen.getByLabelText("Cotización (ARS por USD)"), {
      target: { value: "1500" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Acreditar pago" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Corresponde renovar.");
    expect(svc.applyPayment).toHaveBeenCalledWith(
      "t-1",
      expect.objectContaining({ amount_ars: "40500", exchange_rate: "1500" }),
    );
  });

  it("cancelar la renovación pide confirmación explícita", async () => {
    svc.get.mockResolvedValue(DETALLE);
    svc.cancelRenewal.mockResolvedValue({ access_until: "2026-12-01T00:00:00Z" });
    conCliente(<TenantDetailScreen tenantId="t-1" />);

    fireEvent.click(await screen.findByRole("button", { name: /Cancelar renovación/ }));
    expect(svc.cancelRenewal).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Confirmar cancelación" }));
    await waitFor(() => expect(svc.cancelRenewal).toHaveBeenCalledWith("t-1", null, ""));
  });
});
