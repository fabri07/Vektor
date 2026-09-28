/**
 * Registrar y DESREGISTRAR el service worker de la caja (B7b).
 *
 * El apagado importa tanto como el encendido: es el kill switch. Si el dueño
 * desmarca la casilla, o si el navegador no soporta service workers, la PC
 * tiene que quedar exactamente como estaba — sin registro y sin caché.
 */

const RUTA = "/sw.js";
/** El mismo nombre que usa `public/sw.js`. */
const PREFIJO_CACHE = "vektor-pos-";

function soportado(): boolean {
  return typeof navigator !== "undefined" && "serviceWorker" in navigator;
}

export async function registrarServiceWorker(): Promise<ServiceWorkerRegistration | null> {
  if (!soportado()) return null;
  try {
    return await navigator.serviceWorker.register(RUTA, { scope: "/" });
  } catch {
    // Sin HTTPS, o el navegador lo bloqueó: la caja sigue andando online.
    return null;
  }
}

export async function desregistrarServiceWorker(): Promise<void> {
  if (!soportado()) return;
  try {
    for (const registro of await navigator.serviceWorker.getRegistrations()) {
      // Sólo el de la caja: otro registro del mismo origen no es asunto nuestro.
      if (registro.active?.scriptURL.endsWith(RUTA) || registro.installing || registro.waiting) {
        await registro.unregister();
      }
    }
    if (typeof caches !== "undefined") {
      for (const nombre of await caches.keys()) {
        if (nombre.startsWith(PREFIJO_CACHE)) await caches.delete(nombre);
      }
    }
  } catch {
    // Nada que hacer: apagar no puede romper la caja.
  }
}

export interface EstadoSinInternet {
  /** `true` sólo si el documento y TODOS los recursos declarados están guardados. */
  lista: boolean;
  faltan: string[];
}

/** Los `/_next/static/*` que esta pantalla cargó de verdad, chunks dinámicos incluidos. */
function recursosDeEstaPagina(): string[] {
  if (typeof performance === "undefined" || !performance.getEntriesByType) return [];
  const propios = performance
    .getEntriesByType("resource")
    .map((e) => e.name)
    .filter((url) => url.startsWith(window.location.origin) && url.includes("/_next/static/"));
  return [...new Set(propios)];
}

/**
 * Registra el service worker y **verifica** que la PC pueda abrir sin red.
 *
 * Registrar no es estar listo: los chunks que la pantalla ya tenía cargados se
 * pidieron antes de que el service worker existiera, así que no están en su
 * caché. Si se dijera "PC preparada" ahí, un reinicio sin red abriría una
 * pantalla en blanco. Acá se le pasan los recursos que esta pantalla cargó de
 * verdad y se espera su confirmación.
 */
export async function prepararParaSinInternet(): Promise<EstadoSinInternet> {
  const registro = await registrarServiceWorker();
  if (!registro) return { lista: false, faltan: [] };
  let activo: ServiceWorker | null = null;
  try {
    activo = (await navigator.serviceWorker.ready).active;
  } catch {
    activo = registro.active;
  }
  if (!activo) return { lista: false, faltan: [] };

  return new Promise<EstadoSinInternet>((resolve) => {
    const canal = new MessageChannel();
    // Si el service worker no contesta, NO se afirma que la PC está lista.
    const reloj = setTimeout(() => {
      canal.port1.close();
      resolve({ lista: false, faltan: [] });
    }, 20_000);
    canal.port1.onmessage = (evento) => {
      clearTimeout(reloj);
      const datos = (evento.data ?? {}) as Partial<EstadoSinInternet>;
      canal.port1.close();
      resolve({ lista: Boolean(datos.lista), faltan: datos.faltan ?? [] });
    };
    activo.postMessage({ type: "PREPARAR", urls: recursosDeEstaPagina() }, [canal.port2]);
  });
}

/**
 * Activa la versión nueva SÓLO cuando es seguro.
 *
 * `skipWaiting` a secas mezcla código nuevo con una página vieja, y en una caja
 * eso puede pasar en medio de un cobro. La página llama a esto con el carrito
 * vacío y sin envíos en vuelo.
 */
export async function activarVersionNueva(): Promise<boolean> {
  if (!soportado()) return false;
  const registro = await navigator.serviceWorker.getRegistration(RUTA);
  if (!registro?.waiting) return false;
  registro.waiting.postMessage({ type: "SKIP_WAITING" });
  return true;
}
