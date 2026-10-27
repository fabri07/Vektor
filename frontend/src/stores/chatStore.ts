import type { AutomationOffer } from "@/services/automations.service";
import { create } from "zustand";
import { persist } from "zustand/middleware";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  status?: "success" | "requires_approval" | "requires_clarification" | "error" | "requires_google_auth";
  pendingActionId?: string;
  pendingActionIds?: string[];    // Stage 3: grupo multi-task
  approvalGroupId?: string;       // Stage 3: vincula PAs del grupo
  groupResult?: {                 // Stage 3: progreso post-confirmación
    groupExecutionStatus: "SUCCEEDED" | "PARTIAL_FAILED";
    tasks: Array<{ action_id: string; action_type: string; execution_status: string }>;
  };
  automationOffer?: AutomationOffer;
  timestamp: string; // ISO string — Date no es serializable en localStorage
  requiresGoogleAuth?: boolean;
  authUrl?: string;
}

// Dueño de la conversación guardada (X6-2): sin esto, el siguiente usuario de
// la misma PC veía la conversación del anterior y la seguía usando.
export interface ChatOwner {
  userId: string;
  tenantId: string;
}

function ownerKey(owner: ChatOwner): string {
  return `${owner.tenantId}:${owner.userId}`;
}

interface ChatState {
  conversationId: string;
  messages: ChatMessage[];
  ownerKey: string | null;
  addMessage: (msg: Omit<ChatMessage, "id" | "timestamp">) => void;
  addMessageWithId: (msg: ChatMessage) => void;
  updateMessage: (id: string, patch: Partial<ChatMessage>) => void;
  newConversation: () => void;
  loadMessages: (conversationId: string, messages: ChatMessage[]) => void;
  /** Ata la conversación al usuario de la sesión: si es otro (o no se sabe de
   * quién era), la descarta y arranca una nueva. Con `null` (logout) la borra. */
  bindOwner: (owner: ChatOwner | null) => void;
}

export const useChatStore = create<ChatState>()(
  persist(
    (set, get) => ({
      conversationId: crypto.randomUUID(),
      messages: [],
      ownerKey: null,

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

      addMessageWithId: (msg) =>
        set((state) => ({
          messages: [...state.messages, msg],
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

      bindOwner: (owner) => {
        if (owner === null) {
          set({ conversationId: crypto.randomUUID(), messages: [], ownerKey: null });
          useChatStore.persist.clearStorage();
          return;
        }
        const key = ownerKey(owner);
        if (get().ownerKey === key) return;
        // Otro usuario, o una conversación guardada sin dueño (anterior a X6-2):
        // no se puede saber si es suya, así que se descarta.
        set({ conversationId: crypto.randomUUID(), messages: [], ownerKey: key });
      },
    }),
    {
      name: "vektor_chat",
      partialize: (state) => ({
        conversationId: state.conversationId,
        messages: state.messages.slice(-50), // últimos 50 mensajes
        ownerKey: state.ownerKey,
      }),
    },
  ),
);
