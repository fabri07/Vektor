import "@testing-library/jest-dom";
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ChatPanel } from "../ChatPanel";
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
const mockConfirmAction = agentService.confirmAction as jest.MockedFunction<
  typeof agentService.confirmAction
>;
const mockCancelAction = agentService.cancelAction as jest.MockedFunction<
  typeof agentService.cancelAction
>;
const mockGetChatUsage = agentService.getChatUsage as jest.MockedFunction<
  typeof agentService.getChatUsage
>;

function openChat() {
  fireEvent.click(screen.getByLabelText("Abrir asistente"));
}

function renderChat() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <ChatPanel />
    </QueryClientProvider>,
  );
}

describe("ChatPanel", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockGetChatUsage.mockResolvedValue({ messages_today: 0, limit: 50 });
    useChatStore.setState({ conversationId: "test-conversation", messages: [] });
  });

  test("test_initial_messages_shown_when_empty", () => {
    renderChat();
    openChat();
    expect(
      screen.getByText(/Hola! Soy tu asistente de Véktor/i)
    ).toBeInTheDocument();
    expect(screen.getByText(/Hoy vendí 85 mil/i)).toBeInTheDocument();
  });

  test("test_sends_message_and_shows_response", async () => {
    mockSendMessage.mockResolvedValueOnce({
      request_id: "req-1",
      agent_name: "AgentCash",
      status: "success",
      risk_level: "LOW",
      requires_approval: false,
      result: { summary: "Venta registrada correctamente." },
    });

    renderChat();
    openChat();

    const textarea = screen.getByPlaceholderText("Escribí tu mensaje...");
    await userEvent.type(textarea, "Hoy vendí 50 mil");
    fireEvent.click(screen.getByLabelText("Enviar"));

    await waitFor(() => {
      expect(
        screen.getByText("Venta registrada correctamente.")
      ).toBeInTheDocument();
    });
    // Firma real: (message, conversationId?, attachments?, uiContext?). Sin
    // adjuntos ni contexto de UI los dos últimos viajan undefined, pero igual
    // cuentan para la aridad que compara toHaveBeenCalledWith.
    expect(mockSendMessage).toHaveBeenCalledWith(
      "Hoy vendí 50 mil",
      expect.any(String),
      undefined,
      undefined,
    );
  });

  test("test_approval_card_shows_on_requires_approval", async () => {
    mockSendMessage.mockResolvedValueOnce({
      request_id: "req-2",
      agent_name: "AgentCash",
      status: "requires_approval",
      risk_level: "HIGH",
      requires_approval: true,
      pending_action_id: "pending-abc",
      result: { summary: "¿Confirmás registrar venta de $50.000?" },
    });

    renderChat();
    openChat();

    const textarea = screen.getByPlaceholderText("Escribí tu mensaje...");
    await userEvent.type(textarea, "Vendí 50 mil");
    fireEvent.click(screen.getByLabelText("Enviar"));

    await waitFor(() => {
      expect(
        screen.getByText("¿Confirmás registrar venta de $50.000?")
      ).toBeInTheDocument();
    });
    expect(screen.getByText("Confirmar")).toBeInTheDocument();
    expect(screen.getByText("Cancelar")).toBeInTheDocument();
  });

  test("test_confirm_button_calls_confirm_endpoint", async () => {
    mockSendMessage.mockResolvedValueOnce({
      request_id: "req-3",
      agent_name: "AgentCash",
      status: "requires_approval",
      risk_level: "HIGH",
      requires_approval: true,
      pending_action_id: "pending-xyz",
      result: { summary: "¿Confirmás el registro?" },
    });
    mockConfirmAction.mockResolvedValueOnce({
      status: "confirmed",
      action_type: "REGISTER_SALE",
      execution_status: "SUCCEEDED",
    });

    renderChat();
    openChat();

    const textarea = screen.getByPlaceholderText("Escribí tu mensaje...");
    await userEvent.type(textarea, "Vendí 30 mil");
    fireEvent.click(screen.getByLabelText("Enviar"));

    await waitFor(() => {
      expect(screen.getByText("Confirmar")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("Confirmar"));

    await waitFor(() => {
      expect(mockConfirmAction).toHaveBeenCalledWith("pending-xyz");
    });
  });

  test("test_cancel_button_calls_cancel_endpoint", async () => {
    mockSendMessage.mockResolvedValueOnce({
      request_id: "req-4",
      agent_name: "AgentCash",
      status: "requires_approval",
      risk_level: "HIGH",
      requires_approval: true,
      pending_action_id: "pending-cancel",
      result: { summary: "¿Confirmás el registro?" },
    });
    mockCancelAction.mockResolvedValueOnce(undefined);

    renderChat();
    openChat();

    const textarea = screen.getByPlaceholderText("Escribí tu mensaje...");
    await userEvent.type(textarea, "Vendí 20 mil");
    fireEvent.click(screen.getByLabelText("Enviar"));

    await waitFor(() => {
      expect(screen.getByText("Cancelar")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("Cancelar"));

    await waitFor(() => {
      expect(mockCancelAction).toHaveBeenCalledWith("pending-cancel");
    });
  });

  test("test_rate_limit_shows_message_on_429", async () => {
    const error = Object.assign(new Error("Too Many Requests"), {
      response: { status: 429 },
    });
    mockSendMessage.mockRejectedValueOnce(error);

    renderChat();
    openChat();

    const textarea = screen.getByPlaceholderText("Escribí tu mensaje...");
    await userEvent.type(textarea, "Hola");
    fireEvent.click(screen.getByLabelText("Enviar"));

    await waitFor(() => {
      expect(
        screen.getByText(/Límite diario de 50 mensajes alcanzado/i)
      ).toBeInTheDocument();
    });
  });
});
