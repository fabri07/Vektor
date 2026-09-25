import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

import { useSessionSync } from "../useSessionSync";
import { useAuthStore } from "@/stores/authStore";
import { getMeRequest } from "@/services/auth.service";

jest.mock("@/services/auth.service", () => ({ getMeRequest: jest.fn() }));
const mockMe = getMeRequest as jest.Mock;

function me(role: string, id = "u1") {
  return {
    user_id: id,
    email: "a@b.com",
    full_name: "Ana",
    role_code: role,
    tenant_id: "t1",
    phone: null,
    subscription: null,
    onboarding_completed: true,
    pos_permissions: role === "CASHIER" ? ["discount"] : [],
  };
}

function montar(qc: QueryClient) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
  return renderHook(() => useSessionSync(), { wrapper });
}

beforeEach(() => {
  mockMe.mockReset();
  useAuthStore.setState({
    token: "t",
    user: { id: "u1", email: "a@b.com", full_name: "Ana", role: "OWNER", tenant_id: "t1" },
  });
});

describe("useSessionSync", () => {
  it("si el dueño lo pasó a cajero, toma el rol nuevo y vacía la caché", async () => {
    const qc = new QueryClient();
    qc.setQueryData(["costos"], { unit_cost_ars: 60 });
    mockMe.mockResolvedValue(me("CASHIER"));

    const { result } = montar(qc);
    await waitFor(() => expect(result.current).toBe("ok"));

    expect(useAuthStore.getState().user?.role).toBe("CASHIER");
    expect(useAuthStore.getState().user?.pos_permissions).toEqual(["discount"]);
    expect(qc.getQueryData(["costos"])).toBeUndefined();
  });

  it("si nada cambió, no toca la caché", async () => {
    const qc = new QueryClient();
    qc.setQueryData(["ventas"], [1, 2]);
    mockMe.mockResolvedValue(me("OWNER"));

    const { result } = montar(qc);
    await waitFor(() => expect(result.current).toBe("ok"));
    expect(qc.getQueryData(["ventas"])).toEqual([1, 2]);
  });

  it("sin red sigue con el rol guardado", async () => {
    mockMe.mockRejectedValue(new Error("offline"));
    const { result } = montar(new QueryClient());
    await waitFor(() => expect(result.current).toBe("offline"));
    expect(useAuthStore.getState().user?.role).toBe("OWNER");
  });
});
