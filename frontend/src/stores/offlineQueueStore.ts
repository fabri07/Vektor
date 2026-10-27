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
  /**
   * Dueño de la carga (X6-1): el negocio y el usuario de la sesión que la
   * encoló. Sólo esa sesión la sincroniza. La idempotencia del servidor es por
   * (tenant, clave), así que subirla con el JWT de OTRO negocio la hacía entrar
   * en ese negocio. Opcionales porque las colas persistidas antes (version 1)
   * no los tienen: esas son las «sin dueño» (`sinDuenio`).
   */
  tenantId?: string;
  userId?: string;
  /** Quién reclamó una carga sin dueño con «Es mía, subirla», y cuándo (ISO). */
  adoptadaPor?: string;
  adoptadaEn?: string;
}

/** La sesión contra la que se filtra la cola: negocio y usuario del JWT. */
export interface SesionDeCola {
  tenantId: string;
  userId: string;
}

/** `undefined` (cola persistida antes de que existiera `status`) cuenta como pendiente. */
export function isFailed(item: QueuedItem): boolean {
  return item.status === "FAILED";
}

/**
 * Carga encolada antes de X6-1, sin negocio ni usuario. NO se sube sola: no hay
 * forma de saber de quién es, y adivinar la mandaría a lo mejor al negocio de
 * quien esté logueado. Espera que una persona diga «es mía» (`adoptar`) o la
 * descarte.
 */
export function sinDuenio(item: QueuedItem): boolean {
  return !item.tenantId || !item.userId;
}

/** La carga es de esta sesión: mismo negocio Y mismo usuario (como el POS). */
export function esDeLaSesion(item: QueuedItem, sesion: SesionDeCola | null): boolean {
  return (
    sesion !== null &&
    !sinDuenio(item) &&
    item.tenantId === sesion.tenantId &&
    item.userId === sesion.userId
  );
}

interface OfflineQueueState {
  items: QueuedItem[];
  enqueue: (item: QueuedItem) => void;
  /** Sincronizada con éxito, o descartada por una acción explícita del usuario. */
  remove: (id: string) => void;
  /** Fallo reintentable del SERVIDOR: suma un intento y deja el item pendiente. */
  markFailed: (id: string, error: string) => void;
  /**
   * Fallo de RED: deja constancia del error pero **no** consume el presupuesto de
   * reintentos. Un corte no es evidencia de que la operación sea inválida, y
   * `flush` corre una vez por navegación de página: sin esta distinción, cuatro
   * clics durante un corte agotaban el presupuesto y el primer 503 real después
   * mandaba la venta a FAILED sin haberla reintentado nunca de verdad.
   */
  markNetworkFailure: (id: string, error: string) => void;
  /** Fallo terminal: deja de reintentarse pero el item NO se borra. */
  markPermanentlyFailed: (id: string, error: string) => void;
  /** Vuelve a habilitar un item terminal (acción explícita del usuario). */
  retry: (id: string) => void;
  /**
   * Una carga sin dueño (`sinDuenio`) pasa a la sesión dada. Sólo por acción
   * explícita del usuario («son mías, subirlas»); a una carga que ya tiene dueño
   * no la toca nunca, aunque se lo pidan.
   */
  adoptar: (id: string, sesion: SesionDeCola) => void;
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
const CLAVE_COLA = "vektor_offline_queue";

/**
 * Los items vigentes ANTES de escribir: lo que está en `localStorage` mergeado
 * por id con lo que esta pestaña tiene en memoria.
 *
 * Existe porque persist reescribe el array ENTERO: con dos pestañas abiertas, la
 * 2 encolaba o borraba sobre su copia en memoria y pisaba lo que la 1 había
 * encolado —una venta guardada sin red que desaparecía sin que nadie se
 * enterara—. Lo guardado gana para los ids que están en los dos lados (es lo
 * último que escribió cualquier pestaña); lo que sólo está en memoria se
 * conserva. El costo de conservar es acotado: un item que otra pestaña acababa
 * de sincronizar puede volver una vez, y su reenvío vuelve con
 * `DUPLICATE_IDEMPOTENT` y sale de la cola; perderlo no tiene arreglo.
 */
export function itemsVigentes(enMemoria: QueuedItem[]): QueuedItem[] {
  let guardados: QueuedItem[] | null = null;
  try {
    const crudo = typeof localStorage !== "undefined" ? localStorage.getItem(CLAVE_COLA) : null;
    const items = crudo ? (JSON.parse(crudo) as { state?: { items?: unknown } })?.state?.items : null;
    if (Array.isArray(items)) guardados = items as QueuedItem[];
  } catch {
    guardados = null; // storage ilegible o bloqueado: queda lo de memoria
  }
  if (guardados === null) return enMemoria;
  const ids = new Set(guardados.map((i) => i.id));
  return [...guardados, ...enMemoria.filter((i) => !ids.has(i.id))];
}

export const useOfflineQueueStore = create<OfflineQueueState>()(
  persist(
    (set) => {
      // Toda escritura parte de `itemsVigentes`, nunca de la copia en memoria sola.
      const actualizar = (f: (items: QueuedItem[]) => QueuedItem[]) =>
        set((s) => ({ items: f(itemsVigentes(s.items)) }));
      return {
        items: [],
        enqueue: (item) => actualizar((items) => [...items.filter((i) => i.id !== item.id), item]),
        remove: (id) => actualizar((items) => items.filter((i) => i.id !== id)),
        markFailed: (id, error) =>
          actualizar((items) =>
            items.map((i) =>
              i.id === id ? { ...i, attempts: i.attempts + 1, lastError: error } : i,
            ),
          ),
        markNetworkFailure: (id, error) =>
          actualizar((items) => items.map((i) => (i.id === id ? { ...i, lastError: error } : i))),
        markPermanentlyFailed: (id, error) =>
          actualizar((items) =>
            items.map((i) =>
              i.id === id
                ? { ...i, attempts: i.attempts + 1, lastError: error, status: "FAILED" as const }
                : i,
            ),
          ),
        retry: (id) =>
          actualizar((items) =>
            items.map((i) =>
              i.id === id
                ? { ...i, attempts: 0, lastError: undefined, status: "PENDING" as const }
                : i,
            ),
          ),
        adoptar: (id, sesion) =>
          actualizar((items) =>
            items.map((i) =>
              i.id === id && sinDuenio(i)
                ? {
                    ...i,
                    tenantId: sesion.tenantId,
                    userId: sesion.userId,
                    adoptadaPor: sesion.userId,
                    adoptadaEn: new Date().toISOString(),
                  }
                : i,
            ),
          ),
      };
    },
    {
      name: CLAVE_COLA,
      // v2 (X6-1): los items llevan `tenantId`/`userId`. Sin `migrate`, zustand
      // descarta el estado guardado con otra versión: eso era borrar en silencio
      // cargas que alguien dio por guardadas. La migración las conserva TAL CUAL,
      // sin inventarles dueño: quedan «sin dueño» hasta que una persona decida.
      version: 2,
      migrate: (persistido) => {
        const items = (persistido as { items?: unknown } | null)?.items;
        return { items: Array.isArray(items) ? (items as QueuedItem[]) : [] };
      },
      partialize: (s) => ({ items: s.items }),
    },
  ),
);

// Otra pestaña escribió la cola: re-hidratar para que esta muestre (y no pise)
// lo que encoló la otra. El evento `storage` sólo llega a las OTRAS pestañas.
if (typeof window !== "undefined") {
  window.addEventListener("storage", (e) => {
    if (e.key === CLAVE_COLA) void useOfflineQueueStore.persist.rehydrate();
  });
}
