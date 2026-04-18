"use client";

import { useCallback } from "react";
import {
  sendMessage,
  confirmAction,
  cancelAction,
  type AgentResponse,
  type ChatAttachment,
} from "@/services/agent.service";
import { useChatStore, type ChatMessage } from "@/stores/chatStore";
import { type AxiosError } from "axios";
import { useState } from "react";

// Re-exportar para compatibilidad con importadores existentes
export type Message = ChatMessage;

export function useChat() {
  const {
    conversationId,
    messages,
    addMessage: storeAdd,
    updateMessage,
    newConversation,
  } = useChatStore();

  const [isLoading, setIsLoading] = useState(false);
  const [messagesUsedToday, setMessagesUsedToday] = useState(0);
  const [isRateLimited, setIsRateLimited] = useState(false);

  const addMessage = useCallback(
    (msg: Omit<Message, "id" | "timestamp">) => {
      storeAdd(msg);
    },
    [storeAdd],
  );

  const send = useCallback(
    async (
      text: string,
      attachments?: ChatAttachment[],
      displayText?: string,
    ) => {
      const hasText = text.trim().length > 0;
      const hasAttachments = (attachments?.length ?? 0) > 0;
      if ((!hasText && !hasAttachments) || isLoading || isRateLimited) return;

      addMessage({ role: "user", content: displayText ?? text });
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
    [isLoading, isRateLimited, conversationId, addMessage],
  );

  const confirm = useCallback(
    async (pendingActionId: string) => {
      await confirmAction(pendingActionId);
      // Encontrar el mensaje y actualizar su contenido
      const msg = messages.find((m) => m.pendingActionId === pendingActionId);
      if (msg) {
        updateMessage(msg.id, {
          status: "success",
          content: msg.content + "\n✓ Confirmado y guardado.",
        });
      }
    },
    [messages, updateMessage],
  );

  const cancel = useCallback(
    async (pendingActionId: string) => {
      await cancelAction(pendingActionId);
      const msg = messages.find((m) => m.pendingActionId === pendingActionId);
      if (msg) {
        updateMessage(msg.id, {
          status: "error",
          content: msg.content + "\n✗ Cancelado.",
        });
      }
    },
    [messages, updateMessage],
  );

  return {
    messages,
    isLoading,
    send,
    confirm,
    cancel,
    messagesUsedToday,
    isRateLimited,
    newConversation,
  };
}
