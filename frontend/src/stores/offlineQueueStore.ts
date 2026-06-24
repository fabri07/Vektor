import { create } from "zustand";
import { persist } from "zustand/middleware";

export type QueuedKind = "sale" | "expense" | "product" | "sale_batch" | "purchase";

export interface QueuedItem {
  id: string; // UUID — también es la Idempotency-Key del POST
  kind: QueuedKind;
  payload: unknown;
  createdAt: string; // ISO
  attempts: number;
  lastError?: string;
}

interface OfflineQueueState {
  items: QueuedItem[];
  enqueue: (item: QueuedItem) => void;
  remove: (id: string) => void;
  markFailed: (id: string, error: string) => void;
}

/**
 * Cola local de cargas manuales para cuando se cae internet. Persiste en
 * localStorage (patrón de chatStore). `version` permite migrar el shape sin
 * romper colas viejas silenciosamente.
 */
export const useOfflineQueueStore = create<OfflineQueueState>()(
  persist(
    (set) => ({
      items: [],
      enqueue: (item) => set((s) => ({ items: [...s.items, item] })),
      remove: (id) => set((s) => ({ items: s.items.filter((i) => i.id !== id) })),
      markFailed: (id, error) =>
        set((s) => ({
          items: s.items.map((i) =>
            i.id === id ? { ...i, attempts: i.attempts + 1, lastError: error } : i,
          ),
        })),
    }),
    {
      name: "vektor_offline_queue",
      version: 1,
    },
  ),
);
