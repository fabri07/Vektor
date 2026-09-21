import { create } from "zustand";
import { persist } from "zustand/middleware";

export type QueuedKind = "sale" | "expense" | "product" | "sale_batch" | "purchase";

/**
 * `PENDING` — se reintenta en cada flush.
 * `FAILED`  — estado TERMINAL: dejó de reintentarse, pero NO se borra. Queda
 *             visible con su `lastError` hasta que una persona decida qué hacer
 *             (`retry` o `discard`).
 *
 * `undefined` se lee como `PENDING`: las colas ya persistidas con `version: 1`
 * no tienen el campo, y arrancarlas en un estado terminal las congelaría.
 */
export type QueuedStatus = "PENDING" | "FAILED";

export interface QueuedItem {
  id: string; // UUID — también es la Idempotency-Key del POST
  kind: QueuedKind;
  payload: unknown;
  createdAt: string; // ISO
  attempts: number;
  lastError?: string;
  status?: QueuedStatus;
}

/** `undefined` (cola persistida antes de que existiera `status`) cuenta como pendiente. */
export function isFailed(item: QueuedItem): boolean {
  return item.status === "FAILED";
}

interface OfflineQueueState {
  items: QueuedItem[];
  enqueue: (item: QueuedItem) => void;
  /** Sincronizada con éxito, o descartada por una acción explícita del usuario. */
  remove: (id: string) => void;
  /** Fallo reintentable: suma un intento y deja el item pendiente. */
  markFailed: (id: string, error: string) => void;
  /** Fallo terminal: deja de reintentarse pero el item NO se borra. */
  markPermanentlyFailed: (id: string, error: string) => void;
  /** Vuelve a habilitar un item terminal (acción explícita del usuario). */
  retry: (id: string) => void;
}

/**
 * Cola local de cargas manuales para cuando se cae internet. Persiste en
 * localStorage (patrón de chatStore). `version` permite migrar el shape sin
 * romper colas viejas silenciosamente.
 *
 * Invariante: **una operación encolada nunca se borra por cantidad de
 * reintentos.** Antes, al llegar al tope, el item se eliminaba: para una carga
 * manual de a una eso era un ítem perdido, pero para una venta de caja ya
 * cobrada y ticketeada es plata que desaparece sin que nadie se entere. El tope
 * sigue existiendo —un item que falla siempre no puede reintentarse para
 * siempre— pero ahora frena en `FAILED` en vez de borrar.
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
      markPermanentlyFailed: (id, error) =>
        set((s) => ({
          items: s.items.map((i) =>
            i.id === id
              ? { ...i, attempts: i.attempts + 1, lastError: error, status: "FAILED" as const }
              : i,
          ),
        })),
      retry: (id) =>
        set((s) => ({
          items: s.items.map((i) =>
            i.id === id ? { ...i, attempts: 0, lastError: undefined, status: "PENDING" as const } : i,
          ),
        })),
    }),
    {
      name: "vektor_offline_queue",
      version: 1,
    },
  ),
);
