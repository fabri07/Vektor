import { create } from "zustand";
import { persist } from "zustand/middleware";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  status?: "success" | "requires_approval" | "requires_clarification" | "error";
  pendingActionId?: string;
  timestamp: string; // ISO string — Date no es serializable en localStorage
}

interface ChatState {
  conversationId: string;
  messages: ChatMessage[];
  addMessage: (msg: Omit<ChatMessage, "id" | "timestamp">) => void;
  updateMessage: (id: string, patch: Partial<ChatMessage>) => void;
  newConversation: () => void;
  loadMessages: (conversationId: string, messages: ChatMessage[]) => void;
}

export const useChatStore = create<ChatState>()(
  persist(
    (set) => ({
      conversationId: crypto.randomUUID(),
      messages: [],

      addMessage: (msg) =>
        set((state) => ({
          messages: [
            ...state.messages,
            {
              ...msg,
              id: crypto.randomUUID(),
              timestamp: new Date().toISOString(),
            },
          ],
        })),

      updateMessage: (id, patch) =>
        set((state) => ({
          messages: state.messages.map((m) =>
            m.id === id ? { ...m, ...patch } : m,
          ),
        })),

      newConversation: () =>
        set({ conversationId: crypto.randomUUID(), messages: [] }),

      loadMessages: (conversationId, messages) =>
        set({ conversationId, messages }),
    }),
    {
      name: "vektor_chat",
      partialize: (state) => ({
        conversationId: state.conversationId,
        messages: state.messages.slice(-50), // últimos 50 mensajes
      }),
    },
  ),
);
