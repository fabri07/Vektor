"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import axios from "axios";
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import {
  salesService,
  type CreateSalePayload,
  type ManualBatchSalePayload,
} from "@/services/sales.service";
import { expensesService, type CreateExpensePayload } from "@/services/expenses.service";
import { productsService, type CreateProductPayload } from "@/services/products.service";
import { purchasesService, type ManualPurchasePayload } from "@/services/purchases.service";
import {
  useOfflineQueueStore,
  type QueuedKind,
} from "@/stores/offlineQueueStore";

type PayloadFor<K extends QueuedKind> = K extends "sale"
  ? CreateSalePayload
  : K extends "expense"
    ? CreateExpensePayload
    : K extends "product"
      ? CreateProductPayload
      : K extends "sale_batch"
        ? ManualBatchSalePayload
        : ManualPurchasePayload;

interface SubmitCallbacks {
  onSuccess?: () => void; // entró al servidor (o ya estaba: idempotente)
  onQueued?: () => void; // sin conexión → encolado, se sincroniza al volver online
  onError?: () => void; // error real de validación/servidor (4xx/5xx)
}

// Invalida las queries reales de cada dominio (match por prefijo en TanStack).
const INVALIDATE_KEYS: Record<QueuedKind, string[][]> = {
  sale: [["sales-entries"], ["sales-all"], ["sales-date-range"]],
  expense: [["expenses-entries"], ["expenses-all"], ["expenses-date-range"]],
  product: [["products-list"], ["products-all"], ["product-categories"]],
  sale_batch: [
    ["sales-entries"],
    ["sales-all"],
    ["sales-date-range"],
    ["products-list"],
    ["products-all"],
  ],
  purchase: [
    ["expenses-entries"],
    ["expenses-all"],
    ["products-list"],
    ["products-all"],
    ["inventory"],
  ],
};

function invalidate(qc: QueryClient, kind: QueuedKind) {
  for (const queryKey of INVALIDATE_KEYS[kind]) {
    void qc.invalidateQueries({ queryKey });
  }
  void qc.invalidateQueries({ queryKey: ["ingestion-files"] });
}

async function postByKind(
  kind: QueuedKind,
  payload: unknown,
  idempotencyKey: string,
): Promise<void> {
  if (kind === "sale") {
    await salesService.createSale(payload as CreateSalePayload, idempotencyKey);
  } else if (kind === "expense") {
    await expensesService.createExpense(payload as CreateExpensePayload, idempotencyKey);
  } else if (kind === "product") {
    await productsService.createProduct(payload as CreateProductPayload, idempotencyKey);
  } else if (kind === "sale_batch") {
    await salesService.createManualBatch(payload as ManualBatchSalePayload, idempotencyKey);
  } else {
    await purchasesService.createManual(payload as ManualPurchasePayload, idempotencyKey);
  }
}

// Error de red real: axios sin `response` (timeout/abort/DNS/CORS/offline).
function isNetworkError(e: unknown): boolean {
  return axios.isAxiosError(e) && !e.response;
}

// Error de cliente (4xx): permanente — reintentar el mismo payload no lo va a arreglar.
function isClientError(e: unknown): boolean {
  const status = axios.isAxiosError(e) ? e.response?.status : undefined;
  return status !== undefined && status >= 400 && status < 500;
}

// Tope de reintentos para errores transitorios (5xx) — evita loops infinitos en la cola.
const MAX_FLUSH_ATTEMPTS = 5;

// Replay idempotente: el backend ya tiene el registro → 409 con el código del contrato.
function isDuplicate(e: unknown): boolean {
  if (!axios.isAxiosError(e) || e.response?.status !== 409) return false;
  const detail = (e.response.data as { detail?: { code?: string } } | undefined)?.detail;
  return detail?.code === "DUPLICATE_IDEMPOTENT";
}

// Mensaje legible del error para guardarlo en el item de la cola (`lastError`).
function errorMessage(e: unknown): string {
  if (axios.isAxiosError(e)) {
    const status = e.response?.status;
    return status ? `Error ${status} del servidor` : "Sin conexión al sincronizar";
  }
  return "Error desconocido";
}

/** Cantidad de cargas pendientes de sincronizar (para el badge del launcher). */
export function useOfflineQueueCount(): number {
  return useOfflineQueueStore((s) => s.items.length);
}

/** Estado de conexión reactivo (SSR-safe: arranca online y se corrige al montar). */
export function useOnlineStatus(): boolean {
  const [online, setOnline] = useState(true);
  useEffect(() => {
    setOnline(navigator.onLine);
    const on = () => setOnline(true);
    const off = () => setOnline(false);
    window.addEventListener("online", on);
    window.addEventListener("offline", off);
    return () => {
      window.removeEventListener("online", on);
      window.removeEventListener("offline", off);
    };
  }, []);
  return online;
}

/**
 * Submit resiliente a cortes de internet. `submit` intenta el POST con una
 * Idempotency-Key (el id del item); si falla por red, encola y sincroniza solo
 * al volver online. `autoSync: true` (en el launcher, montado una vez por página)
 * registra el listener `online` y dispara el flush inicial.
 */
export function useOfflineSubmit(opts?: { autoSync?: boolean }) {
  const queryClient = useQueryClient();
  const flushingRef = useRef(false);

  const flush = useCallback(async () => {
    if (flushingRef.current) return;
    if (typeof navigator !== "undefined" && !navigator.onLine) return;
    const { items } = useOfflineQueueStore.getState();
    if (items.length === 0) return;
    flushingRef.current = true;
    try {
      for (const item of [...items]) {
        try {
          await postByKind(item.kind, item.payload, item.id);
          useOfflineQueueStore.getState().remove(item.id);
          invalidate(queryClient, item.kind);
        } catch (e) {
          if (isDuplicate(e)) {
            useOfflineQueueStore.getState().remove(item.id);
            invalidate(queryClient, item.kind);
          } else if (isNetworkError(e)) {
            // Sigue offline: registrar el intento y cortar; se reintenta en el próximo flush.
            useOfflineQueueStore.getState().markFailed(item.id, "Sin conexión al sincronizar");
            break;
          } else if (isClientError(e)) {
            // 4xx permanente (payload inválido, stock insuficiente al sincronizar,
            // proveedor borrado, etc.): reintentar el mismo payload no lo arregla.
            // NO borrar en silencio — las operaciones transaccionales (sale_batch /
            // purchase) pueden 400 legítimamente y el usuario creyó que estaban
            // guardadas. Marcar como fallida (queda visible en el badge con su
            // lastError) y recién descartarla tras MAX_FLUSH_ATTEMPTS.
            if (item.attempts + 1 >= MAX_FLUSH_ATTEMPTS) {
              useOfflineQueueStore.getState().remove(item.id);
            } else {
              useOfflineQueueStore.getState().markFailed(item.id, errorMessage(e));
            }
          } else {
            // 5xx u otro error transitorio del servidor: reintentar con tope. Al alcanzar
            // MAX_FLUSH_ATTEMPTS se descarta (evita el poison-item que se reintenta para siempre).
            if (item.attempts + 1 >= MAX_FLUSH_ATTEMPTS) {
              useOfflineQueueStore.getState().remove(item.id);
            } else {
              useOfflineQueueStore.getState().markFailed(item.id, errorMessage(e));
            }
          }
        }
      }
    } finally {
      flushingRef.current = false;
    }
  }, [queryClient]);

  const submit = useCallback(
    async <K extends QueuedKind>(
      kind: K,
      payload: PayloadFor<K>,
      cb?: SubmitCallbacks,
    ): Promise<void> => {
      const id = crypto.randomUUID();
      try {
        await postByKind(kind, payload, id);
        invalidate(queryClient, kind);
        cb?.onSuccess?.();
      } catch (e) {
        if (isDuplicate(e)) {
          invalidate(queryClient, kind);
          cb?.onSuccess?.();
        } else if (isNetworkError(e)) {
          // Persistencia best-effort: si el encolado falla (ej. localStorage lleno),
          // NO confirmamos como guardado → onError (el form no se resetea, no se pierde la carga).
          try {
            useOfflineQueueStore.getState().enqueue({
              id,
              kind,
              payload,
              createdAt: new Date().toISOString(),
              attempts: 0,
            });
            cb?.onQueued?.();
          } catch {
            cb?.onError?.();
          }
        } else {
          cb?.onError?.();
        }
      }
    },
    [queryClient],
  );

  useEffect(() => {
    if (!opts?.autoSync) return;
    void flush();
    function onOnline() {
      void flush();
    }
    window.addEventListener("online", onOnline);
    return () => window.removeEventListener("online", onOnline);
  }, [opts?.autoSync, flush]);

  return { submit, flush };
}
