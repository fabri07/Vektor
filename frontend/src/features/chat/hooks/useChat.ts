"use client";

import { useCallback, useEffect, useState } from "react";
import {
  sendMessage,
  confirmAction,
  cancelAction,
  getChatUsage,
  type AgentResponse,
  type ChatAttachment,
} from "@/services/agent.service";
import { useChatStore, type ChatMessage } from "@/stores/chatStore";
import { useAuthStore } from "@/stores/authStore";
import { type AxiosError } from "axios";
import { useQueryClient } from "@tanstack/react-query";

const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000") + "/api/v1";

const MUTATING_ACTIONS = new Set([
  "REGISTER_SALE",
  "REGISTER_EXPENSE",
  "REGISTER_PURCHASE",
  "REGISTER_CASH_INFLOW",
  "REGISTER_CASH_OUTFLOW",
  "UPDATE_STOCK",
  "REGISTER_STOCK_LOSS",
]);

// Re-exportar para compatibilidad con importadores existentes
export type Message = ChatMessage;

export function useChat() {
  const {
    conversationId,
    messages,
    addMessage: storeAdd,
    addMessageWithId,
    updateMessage,
    newConversation,
  } = useChatStore();

  const queryClient = useQueryClient();
  const [isLoading, setIsLoading] = useState(false);
  const [messagesUsedToday, setMessagesUsedToday] = useState(0);
  const [isRateLimited, setIsRateLimited] = useState(false);

  useEffect(() => {
    getChatUsage()
      .then((data) => {
        setMessagesUsedToday(data.messages_today);
        if (data.messages_today >= data.limit) setIsRateLimited(true);
      })
      .catch(() => {});
  }, []);

  const addMessage = useCallback(
    (msg: Omit<Message, "id" | "timestamp">) => {
      storeAdd(msg);
    },
    [storeAdd],
  );

  const invalidateAffectedQueries = useCallback(async (actionType?: unknown) => {
    if (typeof actionType !== "string" || !MUTATING_ACTIONS.has(actionType)) {
      return;
    }

    await queryClient.invalidateQueries({ queryKey: ["sales-entries"] });
    await queryClient.invalidateQueries({ queryKey: ["expenses-entries"] });
    await queryClient.invalidateQueries({ queryKey: ["products"] });
    await queryClient.invalidateQueries({ queryKey: ["inventory"] });
    void queryClient.invalidateQueries({ queryKey: ["health-scores"] });
  }, [queryClient]);

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

        if (response.status === "requires_google_auth") {
          addMessage({
            role: "assistant",
            content:
              response.message ??
              response.result?.message ??
              "Para continuar necesito acceso a Google.",
            status: "requires_google_auth",
            requiresGoogleAuth: true,
            authUrl: response.result?.auth_url as string | undefined,
          });
        } else if (response.status === "requires_approval") {
          addMessage({
            role: "assistant",
            content:
              response.message ??
              response.result?.summary ??
              "Requiere tu aprobación.",
            status: "requires_approval",
            pendingActionId: response.pending_action_id,
          });
        } else if (response.status === "requires_clarification") {
          addMessage({
            role: "assistant",
            content:
              response.message ??
              response.question ??
              "¿Podés darme más detalles?",
            status: "requires_clarification",
          });
        } else {
          addMessage({
            role: "assistant",
            content: response.message ?? response.result?.summary ?? "Listo.",
            status: "success",
          });
          await invalidateAffectedQueries(response.result?.action_type);
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
    [isLoading, isRateLimited, conversationId, addMessage, invalidateAffectedQueries],
  );

  const sendStream = useCallback(
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

      // Agregar mensaje de "pensando" que será reemplazado por la respuesta final
      const thinkingId = crypto.randomUUID();
      addMessageWithId({
        id: thinkingId,
        role: "assistant",
        content: "...",
        status: "success",
        timestamp: new Date().toISOString(),
      });

      try {
        const token = useAuthStore.getState().token;
        const response = await fetch(`${API_URL}/agent/chat/stream`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            ...(token ? { Authorization: `Bearer ${token}` } : {}),
          },
          body: JSON.stringify({
            message: text,
            conversation_id: conversationId,
            attachments: attachments ?? [],
          }),
        });

        if (!response.ok || !response.body) {
          throw new Error(`HTTP ${response.status}`);
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let finalResponseAdded = false;

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          const chunk = decoder.decode(value, { stream: true });
          const lines = chunk.split("\n").filter((l) => l.startsWith("data: "));

          for (const line of lines) {
            const raw = line.slice(6).trim();
            if (raw === "[DONE]") break;

            try {
              const event = JSON.parse(raw) as {
                type: string;
                text?: string;
                data?: AgentResponse;
                message?: string;
                code?: number;
              };

              if (event.type === "thinking" && event.text) {
                // Actualizar el mensaje de "pensando" con el texto real
                updateMessage(thinkingId, { content: event.text });
              } else if (event.type === "response" && event.data) {
                const agentResp = event.data;
                setMessagesUsedToday((prev) => prev + 1);
                finalResponseAdded = true;

                if (agentResp.status === "requires_google_auth") {
                  updateMessage(thinkingId, {
                    content:
                      agentResp.message ??
                      agentResp.result?.message ??
                      "Para continuar necesito acceso a Google.",
                    status: "requires_google_auth",
                    requiresGoogleAuth: true,
                    authUrl: agentResp.result?.auth_url as string | undefined,
                  });
                } else if (agentResp.requires_approval) {
                  updateMessage(thinkingId, {
                    content:
                      agentResp.message ??
                      agentResp.result?.summary ??
                      "Requiere tu aprobación.",
                    status: "requires_approval",
                    pendingActionId: agentResp.pending_action_id,
                  });
                } else if (agentResp.status === "requires_clarification") {
                  updateMessage(thinkingId, {
                    content:
                      agentResp.message ??
                      agentResp.question ??
                      "¿Podés darme más detalles?",
                    status: "requires_clarification",
                  });
                } else {
                  updateMessage(thinkingId, {
                    content:
                      agentResp.message ?? agentResp.result?.summary ?? "Listo.",
                    status: "success",
                  });
                  await invalidateAffectedQueries(agentResp.result?.action_type);
                }
              } else if (event.type === "error") {
                finalResponseAdded = true;
                if (event.code === 429) setIsRateLimited(true);
                updateMessage(thinkingId, {
                  role: "system",
                  content: event.message ?? "Hubo un error. Intentá de nuevo.",
                  status: "error",
                });
              }
            } catch {
              // línea malformada — ignorar
            }
          }
        }

        // Si el stream terminó sin respuesta final (ej. desconexión), limpiar placeholder
        if (!finalResponseAdded) {
          updateMessage(thinkingId, {
            content: "Sin respuesta. Intentá de nuevo.",
            status: "error",
          });
        }
      } catch (err) {
        const error = err as AxiosError;
        if (error?.response?.status === 429 || (err as Error)?.message?.includes("429")) {
          setIsRateLimited(true);
          updateMessage(thinkingId, {
            role: "system",
            content: "Límite diario de 50 mensajes alcanzado. Disponible mañana.",
            status: "error",
          });
        } else {
          updateMessage(thinkingId, {
            role: "system",
            content: "Hubo un error. Intentá de nuevo.",
            status: "error",
          });
        }
      } finally {
        setIsLoading(false);
      }
    },
    [isLoading, isRateLimited, conversationId, addMessage, addMessageWithId, updateMessage, invalidateAffectedQueries],
  );

  const confirm = useCallback(
    async (pendingActionId: string) => {
      const response = await confirmAction(pendingActionId);
      const msg = messages.find((m) => m.pendingActionId === pendingActionId);
      if (msg) {
        if (response.execution_status === "SUCCEEDED") {
          updateMessage(msg.id, {
            status: "success",
            content: msg.content + "\n✓ Confirmado y guardado.",
          });
        } else if (response.execution_status === "REQUIRES_RECONNECT") {
          updateMessage(msg.id, {
            status: "requires_google_auth",
            requiresGoogleAuth: true,
            content:
              msg.content +
              "\nNecesito que conectes Google para completar esta acción desde Véktor.",
          });
        } else {
          updateMessage(msg.id, {
            status: "error",
            content:
              msg.content +
              "\nNo se pudo completar la acción externa. Podés reintentarlo cuando el servicio esté disponible.",
          });
        }
      }
      if (response.execution_status === "SUCCEEDED") {
        await invalidateAffectedQueries(response.action_type);
      }
    },
    [messages, updateMessage, invalidateAffectedQueries],
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
    sendStream,
    confirm,
    cancel,
    messagesUsedToday,
    isRateLimited,
    newConversation,
  };
}
