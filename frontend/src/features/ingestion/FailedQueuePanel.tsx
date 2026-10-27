"use client";

import * as Sentry from "@sentry/nextjs";
import { useAuthStore } from "@/stores/authStore";
import {
  useOfflineQueueStore,
  esDeLaSesion,
  isFailed,
  sinDuenio,
  type QueuedItem,
  type QueuedKind,
} from "@/stores/offlineQueueStore";
import { useOfflineAjenasCount, useOfflineSubmit, useSesionDeCola } from "./useOfflineSubmit";

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
 *
 * X6-1: sólo muestra lo de la sesión actual (negocio + usuario). En una PC
 * compartida, el usuario del negocio B no ve —ni puede reintentar ni descartar—
 * lo que cargó sin red el del negocio A. Las cargas viejas sin dueño van aparte
 * (`SinDuenioPanel`).
 */
export function FailedQueuePanel() {
  return (
    <>
      <AjenasAviso />
      <SinDuenioPanel />
      <RechazadasPanel />
    </>
  );
}

/**
 * Lo que cargó otro usuario u otro negocio en esta PC no se muestra ni se
 * sincroniza, pero tampoco puede quedar invisible: se avisa que existe, sin
 * nada de su contenido, para que nadie borre el navegador creyendo que no hay
 * nada pendiente.
 */
function AjenasAviso() {
  const ajenas = useOfflineAjenasCount();
  if (ajenas === 0) return null;
  return (
    <p className="mb-4 rounded-lg border border-vk-border-w bg-vk-bg-light px-3 py-2 text-xs text-vektor-body">
      Hay {ajenas} carga(s) de otro usuario guardadas en esta computadora. Se sincronizan cuando
      ese usuario vuelva a entrar.
    </p>
  );
}

const ROLES_QUE_DECIDEN = new Set(["OWNER", "ADMIN"]);

function numero(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function texto(v: unknown): string | null {
  return typeof v === "string" && v.trim() ? v.trim() : null;
}

/**
 * Un resumen reconocible de la carga para el confirm de «Es mía»: tipo, fecha,
 * monto y descripción. Sólo se muestra a quien la va a reclamar, en el momento
 * de decidir; sin esto se adoptaba a ciegas.
 */
export function resumenDeCarga(item: QueuedItem): string {
  const p = (item.payload ?? {}) as Record<string, unknown>;
  const fecha =
    texto(p.transaction_date) ?? texto(p.expense_date) ?? cuando(item.createdAt) ?? "";
  const lineas = Array.isArray(p.items) ? p.items : Array.isArray(p.lines) ? p.lines : null;
  const monto = numero(p.amount) ?? numero(p.sale_price_ars);
  const descripcion =
    texto(p.description) ?? texto(p.notes) ?? texto(p.name) ?? texto(p.supplier_name) ?? texto(p.category_label);
  return [
    ETIQUETA[item.kind],
    fecha,
    monto !== null ? `$ ${monto.toLocaleString("es-AR")}` : null,
    lineas ? `${lineas.length} línea(s)` : null,
    descripcion,
  ]
    .filter((x): x is string => Boolean(x))
    .join(" · ");
}

function RechazadasPanel() {
  // Se selecciona `items` (referencia estable) y se filtra acá: un selector que
  // devuelve `s.items.filter(...)` construye un array nuevo en cada render y
  // Zustand lo compara por identidad → re-render infinito.
  const items = useOfflineQueueStore((s) => s.items);
  const retry = useOfflineQueueStore((s) => s.retry);
  // Reintentar es VOLVER A MANDAR, no sólo cambiar el estado: sin el flush la
  // carga desaparecía del panel y no se enviaba hasta la próxima navegación.
  const { flush } = useOfflineSubmit();
  const remove = useOfflineQueueStore((s) => s.remove);
  const sesion = useSesionDeCola();
  const failed = items.filter((i) => isFailed(i) && esDeLaSesion(i, sesion));

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

/**
 * Cargas encoladas antes de X6-1: no tienen negocio ni usuario, así que el flush
 * no las sube solas (podrían ser de otro negocio de esta PC). Decisión mínima
 * segura: se muestran a quien esté logueado —sólo tipo, fecha y error, como las
 * rechazadas, nunca el contenido— con dos salidas explícitas y con confirmación:
 * «Es mía, subirla» (las adopta esta sesión y las manda) o «Descartar». Nada
 * las borra ni las asigna solo.
 */
function SinDuenioPanel() {
  const items = useOfflineQueueStore((s) => s.items);
  const adoptar = useOfflineQueueStore((s) => s.adoptar);
  const remove = useOfflineQueueStore((s) => s.remove);
  const { flush } = useOfflineSubmit();
  const sesion = useSesionDeCola();
  const rol = useAuthStore((s) => s.user?.role);
  if (sesion === null) return null;
  // Reclamar o descartar una carga de dueño desconocido es una decisión del
  // negocio, no del que está en la caja: sólo OWNER/ADMIN.
  const puedeDecidir = rol !== undefined && ROLES_QUE_DECIDEN.has(rol);
  const huerfanas: QueuedItem[] = items.filter(sinDuenio);
  if (huerfanas.length === 0) return null;

  return (
    <div className="mb-4 rounded-lg border border-vk-warning/30 bg-vk-warning-bg px-3 py-2.5">
      <p className="mb-1 text-xs font-semibold text-vk-warning">
        {huerfanas.length} carga(s) guardada(s) sin conexión sin usuario asignado
      </p>
      <p className="mb-2 text-xs text-vektor-body">
        Se guardaron en esta computadora antes de que la cola registrara quién las cargó. No se
        suben solas: subilas sólo si las cargaste vos, en este negocio.
        {!puedeDecidir && " Sólo el dueño o un administrador puede subirlas o descartarlas."}
      </p>
      <ul className="flex flex-col gap-2">
        {huerfanas.map((item) => (
          <li
            key={item.id}
            className="flex flex-wrap items-center justify-between gap-2 rounded-md bg-vk-bg-light px-2.5 py-2 text-xs"
          >
            <span className="min-w-0 text-vektor-body">
              <span className="font-medium">{ETIQUETA[item.kind]}</span>
              {item.createdAt && <span className="text-vektor-muted"> · {cuando(item.createdAt)}</span>}
              {item.lastError && <span className="block text-vk-danger">{item.lastError}</span>}
            </span>
            {puedeDecidir && (
              <span className="flex shrink-0 gap-1.5">
                <button
                  type="button"
                  onClick={() => {
                    if (
                      window.confirm(
                        `${resumenDeCarga(item)}\n\nEsta carga se va a registrar en el negocio con el que entraste ahora, a tu nombre. Hacelo sólo si la cargaste vos acá. ¿Seguro?`,
                      )
                    ) {
                      adoptar(item.id, sesion);
                      // Rastro para soporte, SIN el payload (puede tener datos del negocio).
                      Sentry.addBreadcrumb({
                        category: "cola-offline",
                        message: "carga sin dueño adoptada",
                        data: { id: item.id, kind: item.kind },
                      });
                      void flush();
                    }
                  }}
                  className="rounded-md border border-vk-border-w px-2 py-1 font-medium text-vektor-body hover:bg-vk-bg"
                >
                  Es mía, subirla
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
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
