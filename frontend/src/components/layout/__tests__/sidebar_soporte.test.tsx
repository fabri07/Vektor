import "@testing-library/jest-dom";
import React from "react";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { Sidebar } from "@/components/layout/Sidebar";
import { useAuthStore } from "@/stores/authStore";

let mockPathname = "/admin";
jest.mock("next/navigation", () => ({ usePathname: () => mockPathname }));
jest.mock("@/lib/api", () => ({
  api: { get: jest.fn().mockResolvedValue({ data: { display_name: "Kiosco Pepe" } }) },
}));
jest.mock("@/features/ingestion/ManualEntryLauncher", () => ({
  ManualEntryLauncher: () => <button>Cargar manual</button>,
}));

function montar(role: string, pathname = "/admin") {
  mockPathname = pathname;
  useAuthStore.setState({ user: { role, full_name: "Fabrizio", email: "f@x.com" } as never });
  render(
    <QueryClientProvider client={new QueryClient()}>
      <Sidebar mobileOpen={false} onClose={() => undefined} />
    </QueryClientProvider>,
  );
}

const DE_NEGOCIO = ["Dashboard", "Ventas", "Gastos", "Productos", "Clientes", "Proveedores", "Cargar datos"];

describe("Sidebar", () => {
  it("el soporte ve sólo la consola de la plataforma", () => {
    montar("SUPERADMIN");
    for (const item of ["Monitor", "Negocios", "Uso & costos"]) {
      expect(screen.getByRole("link", { name: item })).toBeInTheDocument();
    }
    for (const item of [...DE_NEGOCIO, "Caja"]) {
      expect(screen.queryByRole("link", { name: item })).not.toBeInTheDocument();
    }
    expect(screen.queryByText("Cargar manual")).not.toBeInTheDocument();
    expect(screen.queryByText("Mi negocio")).not.toBeInTheDocument();
  });

  it("el Monitor sólo queda marcado en /admin, no en sus subpantallas", () => {
    montar("SUPERADMIN", "/admin/tenants");
    expect(screen.getByRole("link", { name: "Negocios" })).toHaveClass("bg-vk-surface-1");
    expect(screen.getByRole("link", { name: "Monitor" })).not.toHaveClass("bg-vk-surface-1");
  });

  it("un dueño sigue viendo su menú de siempre y ninguno de la consola", () => {
    montar("OWNER", "/dashboard");
    for (const item of [...DE_NEGOCIO, "Caja"]) {
      expect(screen.getByRole("link", { name: item })).toBeInTheDocument();
    }
    for (const item of ["Monitor", "Negocios", "Uso & costos"]) {
      expect(screen.queryByRole("link", { name: item })).not.toBeInTheDocument();
    }
    expect(screen.getByText("Cargar manual")).toBeInTheDocument();
  });
});
