import React from "react";
import { render, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ChatWidget } from "../ChatWidget";
import { onboardingService } from "@/services/onboarding.service";
import { useAuthStore } from "@/stores/authStore";

const mockReplace = jest.fn();
jest.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mockReplace, push: jest.fn() }),
  usePathname: () => "/admin/tenants",
}));
jest.mock("@/services/onboarding.service");
jest.mock("@/services/agent.service");
jest.mock("@/services/files.service");
jest.mock("../hooks/useChat", () => ({
  useChat: () => ({
    messages: [],
    isLoading: false,
    pendingActions: [],
    pendingActionGroups: [],
    send: jest.fn(),
    sendStream: jest.fn(),
    messagesUsedToday: 0,
    isRateLimited: false,
    newConversation: jest.fn(),
  }),
}));

const mockGetStatus = onboardingService.getStatus as jest.Mock;

function montar(role: string) {
  useAuthStore.setState({ user: { role } as never });
  const qc = new QueryClient();
  render(
    <QueryClientProvider client={qc}>
      <ChatWidget />
    </QueryClientProvider>,
  );
}

describe("ChatWidget — redirección a onboarding", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockGetStatus.mockResolvedValue({ completed: false });
  });

  it("un dueño sin onboarding va a /onboarding", async () => {
    montar("OWNER");
    await waitFor(() => expect(mockReplace).toHaveBeenCalledWith("/onboarding"));
  });

  it("el SUPERADMIN no es redirigido ni se consulta el onboarding", async () => {
    montar("SUPERADMIN");
    await new Promise((r) => setTimeout(r, 20));
    expect(mockGetStatus).not.toHaveBeenCalled();
    expect(mockReplace).not.toHaveBeenCalled();
  });
});
