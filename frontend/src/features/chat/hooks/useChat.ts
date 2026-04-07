"use client";

import { useState, useCallback, useRef } from "react";
import {
  sendMessage,
  confirmAction,
  cancelAction,
  type AgentResponse,
  type ChatAttachment,
} from "@/services/agent.service";
import { type AxiosError } from "axios";

export interface Message {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  status?: AgentResponse["status"];
  pendingActionId?: string;
  timestamp: Date;
}

export function useChat() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  // Stable UUID generated once per hook mount — never replaced by server response
  const conversationId = useRef<string>(crypto.randomUUID()).current;
  const [messagesUsedToday, setMessagesUsedToday] = useState(0);
  const [isRateLimited, setIsRateLimited] = useState(false);

  const addMessage = useCallback((msg: Omit<Message, "id" | "timestamp">) => {
    setMessages((prev) => [
      ...prev,
      { ...msg, id: crypto.randomUUID(), timestamp: new Date() },
    ]);
  }, []);

  const send = useCallback(
    async (text: string, attachments?: ChatAttachment[]) => {
      if (!text.trim() || isLoading || isRateLimited) return;

      addMessage({ role: "user", content: text });
      setIsLoading(true);

      try {
        const response = await sendMessage(text, conversationId, attachments);
        setMessagesUsedToday((prev) => prev + 1);

        if (response.status === "requires_approval") {
          addMessage({
            role: "assistant",
            content: response.result?.summary ?? "Requiere tu aprobación.",
            status: "requires_approval",
            pendingActionId: response.pending_action_id,
          });
        } else if (response.status === "requires_clarification") {
          addMessage({
            role: "assistant",
            content: response.question ?? "¿Podés darme más detalles?",
            status: "requires_clarification",
          });
        } else {
          addMessage({
            role: "assistant",
            content: response.result?.summary ?? "Listo.",
            status: "success",
          });
        }

      } catch (err) {
        const error = err as AxiosError;
        if (error?.response?.status === 429) {
          setIsRateLimited(true);
          addMessage({
            role: "system",
            content:
              "Límite diario de 50 mensajes alcanzado. Disponible mañana.",
            status: "error",
          });
        } else {
          addMessage({
            role: "system",
            content: "Hubo un error. Intentá de nuevo.",
            status: "error",
          });
        }
      } finally {
        setIsLoading(false);
      }
    },
    [isLoading, isRateLimited, conversationId, addMessage] // eslint-disable-line react-hooks/exhaustive-deps
  );

  const confirm = useCallback(
    async (pendingActionId: string) => {
      await confirmAction(pendingActionId);
      setMessages((prev) =>
        prev.map((m) =>
          m.pendingActionId === pendingActionId
            ? {
                ...m,
                status: "success" as const,
                content: m.content + "\n✓ Confirmado y guardado.",
              }
            : m
        )
      );
    },
    []
  );

  const cancel = useCallback(async (pendingActionId: string) => {
    await cancelAction(pendingActionId);
    setMessages((prev) =>
      prev.map((m) =>
        m.pendingActionId === pendingActionId
          ? {
              ...m,
              status: "error" as const,
              content: m.content + "\n✗ Cancelado.",
            }
          : m
      )
    );
  }, []);

  return {
    messages,
    isLoading,
    send,
    confirm,
    cancel,
    messagesUsedToday,
    isRateLimited,
  };
}
