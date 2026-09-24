"use client";

import { useOfflineQueueStore, isFailed, type QueuedKind } from "@/stores/offlineQueueStore";
import { useOfflineSubmit } from "./useOfflineSubmit";

const ETIQUETA: Record<QueuedKind, string> = {
  sale: "Venta",
  expense: "Gasto",
  product: "Producto",
  sale_batch: "Venta multi-producto",
  purchase: "Compra",
};

function cuando(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? ""
    : d.toLocaleString("es-AR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

/**
 * Cargas que el servidor rechazó hasta agotar los reintentos.
 *
 * Existe porque el estado terminal necesita una salida: sin esto una carga
 * fallida se queda en la cola para siempre —contada en el badge, salteada por el
 * flush— y la persona no tiene forma ni de reintentarla ni de sacarla. Borrarla
 * automáticamente tampoco sirve: puede ser plata que alguien dio por guardada.
 *
 * El descarte es explícito y pide confirmación, porque es la única operación de
 * toda la cola que destruye un registro.
 */
export function FailedQueuePanel() {
  // Se selecciona `items` (referencia estable) y se filtra acá: un selector que
  // devuelve `s.items.filter(...)` construye un array nuevo en cada render y
  // Zustand lo compara por identidad → re-render infinito.
  const items = useOfflineQueueStore((s) => s.items);
  const retry = useOfflineQueueStore((s) => s.retry);
  // Reintentar es VOLVER A MANDAR, no sólo cambiar el estado: sin el flush la
  // carga desaparecía del panel y no se enviaba hasta la próxima navegación.
  const { flush } = useOfflineSubmit();
  const remove = useOfflineQueueStore((s) => s.remove);
  const failed = items.filter(isFailed);

  if (failed.length === 0) return null;

  return (
    <div className="mb-4 rounded-lg border border-vk-danger/30 bg-vk-danger/5 px-3 py-2.5">
      <p className="mb-2 text-xs font-semibold text-vk-danger">
        {failed.length} carga(s) que el servidor rechazó
      </p>
      <ul className="flex flex-col gap-2">
        {failed.map((item) => (
          <li
            key={item.id}
            className="flex flex-wrap items-center justify-between gap-2 rounded-md bg-vk-bg-light px-2.5 py-2 text-xs"
          >
            <span className="min-w-0 text-vektor-body">
              <span className="font-medium">{ETIQUETA[item.kind]}</span>
              {item.createdAt && <span className="text-vektor-muted"> · {cuando(item.createdAt)}</span>}
              {item.lastError && <span className="block text-vk-danger">{item.lastError}</span>}
            </span>
            <span className="flex shrink-0 gap-1.5">
              <button
                type="button"
                onClick={() => {
                  retry(item.id);
                  void flush();
                }}
                className="rounded-md border border-vk-border-w px-2 py-1 font-medium text-vektor-body hover:bg-vk-bg"
              >
                Reintentar
              </button>
              <button
                type="button"
                onClick={() => {
                  if (window.confirm("Se va a descartar esta carga y no se puede recuperar. ¿Seguro?")) {
                    remove(item.id);
                  }
                }}
                className="rounded-md border border-vk-danger/40 px-2 py-1 font-medium text-vk-danger hover:bg-vk-danger/10"
              >
                Descartar
              </button>
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
