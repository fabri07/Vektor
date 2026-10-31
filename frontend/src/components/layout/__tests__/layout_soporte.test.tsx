import "@testing-library/jest-dom";
import React from "react";
import { render, screen, waitFor } from "@testing-library/react";

import ProtectedLayout from "@/app/(protected)/layout";
import { useAuthStore } from "@/stores/authStore";

const mockReplace = jest.fn();
let mockPathname = "/dashboard";
jest.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mockReplace, push: jest.fn() }),
  usePathname: () => mockPathname,
}));
jest.mock("@/hooks/useSessionSync", () => ({ useSessionSync: () => "ok" }));
jest.mock("@/components/layout/Sidebar", () => ({ Sidebar: () => <nav>sidebar</nav> }));
jest.mock("@/components/layout/Header", () => ({ Header: () => <header>header</header> }));
jest.mock("@/components/layout/AccountBlockNotice", () => ({ AccountBlockNotice: () => null }));
jest.mock("@/components/dashboard/EconomicTicker", () => ({ EconomicTicker: () => null }));
jest.mock("@/features/notifications/ActionNotificationToast", () => ({ ActionNotificationToast: () => null }));
jest.mock("@/components/ui/PinGateModal", () => ({ PinGateModal: () => null }));
jest.mock("@/features/chat/ChatWidget", () => ({ ChatWidget: () => <div>chat-flotante</div> }));

function montar(role: string, pathname: string) {
  mockPathname = pathname;
  useAuthStore.setState({ token: "t", user: { role } as never });
  render(
    <ProtectedLayout>
      <p>contenido</p>
    </ProtectedLayout>,
  );
}

beforeEach(() => mockReplace.mockClear());

describe("ProtectedLayout y el soporte de la plataforma", () => {
  it("lo saca de una pantalla de negocio hacia /admin y no la renderiza", async () => {
    montar("SUPERADMIN", "/dashboard");
    await waitFor(() => expect(mockReplace).toHaveBeenCalledWith("/admin"));
    expect(screen.queryByText("contenido")).not.toBeInTheDocument();
  });

  it("en la consola no hay chat flotante y no redirige", () => {
    montar("SUPERADMIN", "/admin/tenants");
    expect(screen.getByText("contenido")).toBeInTheDocument();
    expect(screen.queryByText("chat-flotante")).not.toBeInTheDocument();
    expect(mockReplace).not.toHaveBeenCalled();
  });

  it("un dueño conserva su pantalla y su chat", () => {
    montar("OWNER", "/dashboard");
    expect(screen.getByText("contenido")).toBeInTheDocument();
    expect(screen.getByText("chat-flotante")).toBeInTheDocument();
    expect(mockReplace).not.toHaveBeenCalled();
  });
});
