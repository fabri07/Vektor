// Service worker del spike B1. Descartable: lo que se conserva de B1 son
// cinco mediciones, no este archivo. El de producción (B7) activa en un
// momento seguro en vez de `skipWaiting`, porque ahí una activación en
// medio de un cobro deja una caja inconsistente; acá no hay cobro que
// romper y lo que se quiere medir es el arranque, así que toma el control
// lo antes posible.
// ⚠️ SUBIR ESTA VERSIÓN ante cualquier cambio en index.html. El navegador
// re-chequea sw.js en cada navegación, pero si sólo cambia la página y este
// archivo queda byte a byte igual, no hay actualización y el cache sigue
// sirviendo la versión vieja para siempre.
const CACHE = "pos-spike-v3";

// `/pos-spike/` (sin index.html) NO se cachea a propósito: Next sirve los
// archivos de `public/` por su nombre exacto, así que esa ruta no existe y
// pedirla metería un 404 en el cache que después se sirve como si fuera la
// página. La URL canónica es siempre /pos-spike/index.html.
const RECURSOS = [
  "/pos-spike/index.html",
  "/pos-spike/manifest.json",
  "/pos-spike/icon-192.png",
  "/pos-spike/icon-512.png",
];

self.addEventListener("install", (evento) => {
  evento.waitUntil(
    caches
      .open(CACHE)
      .then((cache) => cache.addAll(RECURSOS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (evento) => {
  evento.waitUntil(
    caches
      .keys()
      .then((nombres) =>
        Promise.all(nombres.filter((n) => n !== CACHE).map((n) => caches.delete(n)))
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (evento) => {
  const url = new URL(evento.request.url);
  if (url.origin !== self.location.origin) return;
  if (!url.pathname.startsWith("/pos-spike/")) return;

  // Cache-first. Es lo correcto para lo que se mide: si el recurso está en
  // disco tiene que servirse SIN tocar la red, que es justamente la
  // condición del paso 1. Una estrategia network-first "con fallback"
  // pasaría el test por el fallback y no probaría nada.
  evento.respondWith(
    caches.match(evento.request, { ignoreSearch: true }).then((enCache) => {
      if (enCache) return enCache;
      // Una navegación a cualquier ruta del scope cae en la página: sin
      // esto, abrir la app instalada tras un redirect deja pantalla blanca.
      if (evento.request.mode === "navigate") {
        return caches.match("/pos-spike/index.html").then((pagina) => pagina || fetch(evento.request));
      }
      return fetch(evento.request);
    })
  );
});
