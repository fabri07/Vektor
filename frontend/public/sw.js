/**
 * Service worker de la caja (B7b). Escrito a mano, sin librerías.
 *
 * Su único trabajo es que `/pos` abra con la PC reiniciada y sin red. Todo lo
 * demás lo deja pasar SIN tocar: la API, los payloads de RSC, cualquier otra
 * ruta de la app. Un service worker que cachea de más sirve chunks viejos a
 * toda la aplicación, y ese es el peor modo de falla de este bloque.
 *
 * Se registra sólo desde la caja, y sólo si el dueño encendió "preparar esta PC
 * para abrir sin internet". Apagarlo lo desregistra y borra su caché.
 *
 * NO llama a `skipWaiting()` solo: una versión nueva activada en medio de un
 * cobro mezcla código nuevo con una página vieja. La página avisa cuándo es
 * seguro (carrito vacío, sin envíos en vuelo) mandando `SKIP_WAITING`.
 */

const CACHE = "vektor-pos-v1";
const DOCUMENTO = "/pos";

/**
 * Qué hace el service worker con un pedido. Es la función que decide TODO, y
 * el test la lee de este mismo archivo: no hay una segunda copia que pueda
 * separarse.
 *
 * - `ignorar`: no se intercepta. La red hace su trabajo como si el service
 *   worker no existiera.
 * - `estatico`: chunk de Next con hash en el nombre → cache-first.
 * - `documento`: la navegación a /pos → red primero, caché como respaldo.
 */
function decidirRuta(url, origenPropio, esNavegacion, aceptaRsc) {
  if (url.origin !== origenPropio) return "ignorar";
  // La API nunca se cachea: una respuesta vieja del servidor de negocio es peor
  // que un error de red, porque parece buena.
  if (url.pathname.startsWith("/api/")) return "ignorar";
  // Los payloads de RSC son el estado del servidor: servir uno viejo mezcla dos
  // versiones de la app en la misma pantalla.
  if (aceptaRsc || url.searchParams.has("_rsc")) return "ignorar";
  if (url.pathname.startsWith("/_next/static/")) return "estatico";
  if (esNavegacion && (url.pathname === "/pos" || url.pathname.startsWith("/pos/"))) {
    return "documento";
  }
  return "ignorar";
}

// Para el test: se lee este archivo y se ejerce ESTA función.
self.__decidirRuta = decidirRuta;

self.addEventListener("install", (evento) => {
  evento.waitUntil(
    (async () => {
      const cache = await caches.open(CACHE);
      // Sólo el documento: los chunks se cachean solos al usarse, y exigirlos
      // acá haría que un cambio de nombre de archivo rompa la instalación.
      try {
        await cache.add(DOCUMENTO);
      } catch {
        // Sin red al instalar, se cachea en la primera visita con conexión.
      }
    })(),
  );
});

self.addEventListener("activate", (evento) => {
  evento.waitUntil(
    (async () => {
      for (const nombre of await caches.keys()) {
        if (nombre !== CACHE) await caches.delete(nombre);
      }
      await self.clients.claim();
    })(),
  );
});

self.addEventListener("message", (evento) => {
  // La página decide cuándo es seguro activar la versión nueva.
  if (evento.data && evento.data.type === "SKIP_WAITING") self.skipWaiting();
});

self.addEventListener("fetch", (evento) => {
  const pedido = evento.request;
  if (pedido.method !== "GET") return;
  const url = new URL(pedido.url);
  const aceptaRsc = pedido.headers.get("RSC") !== null;
  const decision = decidirRuta(
    url,
    self.location.origin,
    pedido.mode === "navigate",
    aceptaRsc,
  );
  if (decision === "ignorar") return;

  if (decision === "estatico") {
    evento.respondWith(
      (async () => {
        const cache = await caches.open(CACHE);
        const guardado = await cache.match(pedido);
        if (guardado) return guardado;
        const respuesta = await fetch(pedido);
        // Los chunks de Next llevan hash en el nombre: guardarlos no puede
        // servir contenido viejo bajo un nombre nuevo.
        if (respuesta.ok) await cache.put(pedido, respuesta.clone());
        return respuesta;
      })(),
    );
    return;
  }

  evento.respondWith(
    (async () => {
      const cache = await caches.open(CACHE);
      try {
        const respuesta = await fetch(pedido);
        if (respuesta.ok) await cache.put(DOCUMENTO, respuesta.clone());
        return respuesta;
      } catch (error) {
        const guardado = await cache.match(DOCUMENTO);
        if (guardado) return guardado;
        throw error;
      }
    })(),
  );
});
