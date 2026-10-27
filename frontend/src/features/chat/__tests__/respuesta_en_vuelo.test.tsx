import React from "react";
import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useChat } from "../hooks/useChat";
import * as agentService from "@/services/agent.service";
import { useChatStore } from "@/stores/chatStore";

jest.mock("@/services/agent.service");
jest.mock("@/lib/api", () => ({
  api: {
    post: jest.fn(),
    interceptors: {
      request: { use: jest.fn() },
      response: { use: jest.fn() },
    },
  },
}));

const mockSendMessage = agentService.sendMessage as jest.MockedFunction<
  typeof agentService.sendMessage
>;
const mockGetChatUsage = agentService.getChatUsage as jest.MockedFunction<
  typeof agentService.getChatUsage
>;

function wrapper({ children }: { children: React.ReactNode }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}

// X6-2: una respuesta que llega después de que cambió el dueño de la
// conversación (otro usuario, logout) no se agrega a la conversación nueva.
describe("useChat — respuesta en vuelo", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockGetChatUsage.mockResolvedValue({ messages_today: 0, limit: 50 });
    useChatStore.setState({ conversationId: "conv-de-a", messages: [], ownerKey: "t-a:u-a" });
  });

  function enVuelo(): (msg: string) => void {
    let resolver: (msg: string) => void = () => {};
    mockSendMessage.mockImplementation(
      () =>
        new Promise((res) => {
          resolver = (message) =>
            res({ status: "success", message } as unknown as agentService.AgentResponse);
        }),
    );
    return (msg) => resolver(msg);
  }

  it("con otro dueño a mitad del turno, la respuesta no aparece en la conversación nueva", async () => {
    const responder = enVuelo();
    const { result } = renderHook(() => useChat(), { wrapper });

    let envio: Promise<void> = Promise.resolve();
    act(() => {
      envio = result.current.send("¿cuánto gané?");
    });
    act(() => {
      useChatStore.getState().bindOwner({ userId: "u-b", tenantId: "t-b" });
    });
    await act(async () => {
      responder("el margen de A es 31 %");
      await envio;
    });

    expect(useChatStore.getState().messages).toEqual([]);
  });

  it("sin cambio de dueño, la respuesta se agrega como siempre", async () => {
    const responder = enVuelo();
    const { result } = renderHook(() => useChat(), { wrapper });

    let envio: Promise<void> = Promise.resolve();
    act(() => {
      envio = result.current.send("¿cuánto gané?");
    });
    await act(async () => {
      responder("Ganaste 100");
      await envio;
    });

    const contenidos = useChatStore.getState().messages.map((m) => m.content);
    expect(contenidos).toEqual(["¿cuánto gané?", "Ganaste 100"]);
  });
});
