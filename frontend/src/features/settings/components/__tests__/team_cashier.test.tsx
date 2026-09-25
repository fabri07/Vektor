import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { SecurityPanel } from "../SecurityPanel";
import { useAuthStore } from "@/stores/authStore";
import { securityService } from "@/services/security.service";

jest.mock("@/services/security.service", () => ({
  securityService: {
    pinStatus: jest.fn().mockResolvedValue({ pin_set: true, verified: true, must_set: false }),
    listTeam: jest.fn(),
    setTeamPosPermissions: jest.fn(),
    setTeamPermission: jest.fn(),
  },
}));
jest.mock("@/services/users.service", () => ({ createTeamUserRequest: jest.fn() }));

const mockList = securityService.listTeam as jest.Mock;
const mockSetPos = securityService.setTeamPosPermissions as jest.Mock;

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SecurityPanel />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  useAuthStore.setState({
    token: "t",
    user: { id: "o", email: "o@b.com", full_name: "Dueño", role: "OWNER", tenant_id: "t1" },
  });
  mockList.mockResolvedValue([
    {
      user_id: "c1",
      email: "caja@b.com",
      full_name: "Juan",
      role_code: "CASHIER",
      can_modify_sensitive: false,
      pin_set: false,
      pos_permissions: ["discount"],
    },
  ]);
  mockSetPos.mockReset().mockResolvedValue({});
});

describe("Equipo — cajeros", () => {
  it("muestra al cajero con sus permisos de caja, sin la casilla de modificar datos", async () => {
    renderPanel();
    await act(async () => {
      await userEvent.click(screen.getByRole("button", { name: "Administrar equipo" }));
    });
    await waitFor(() => expect(screen.getByText("Juan")).toBeInTheDocument());
    expect(screen.getByText(/Cajero/)).toBeInTheDocument();
    expect(screen.getByLabelText("Aplicar descuentos")).toBeChecked();
    expect(screen.getByLabelText("Vender fiado")).not.toBeChecked();
    expect(screen.queryByLabelText("Puede modificar datos")).not.toBeInTheDocument();
  });

  it("al marcar un permiso manda el set COMPLETO", async () => {
    renderPanel();
    await act(async () => {
      await userEvent.click(screen.getByRole("button", { name: "Administrar equipo" }));
    });
    await waitFor(() => expect(screen.getByLabelText("Vender fiado")).toBeInTheDocument());
    await act(async () => {
      await userEvent.click(screen.getByLabelText("Vender fiado"));
    });
    expect(mockSetPos).toHaveBeenCalledWith("c1", {
      discount: true,
      fiado: true,
      learn_barcode: false,
      void_ticket: false,
    });
  });
});
