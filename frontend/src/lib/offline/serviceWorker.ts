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
