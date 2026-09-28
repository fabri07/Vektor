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
/** Se borran sólo las cachés de la caja. Ver el handler de `activate`. */
const PREFIJO = "vektor-pos-";
const DOCUMENTO = "/pos";

/** Los chunks de Next que el documento referencia, para el primer arranque. */
function estaticosDelHtml(html) {
  // El HTML de Next trae rutas escapadas (`\/_next\/static\/...`) dentro de los
  // payloads de flight: sin desescapar, el primer arranque se queda sin la
  // mitad de los chunks.
  const plano = html.replace(/\\\//g, "/");
  const encontrados = plano.match(/\/_next\/static\/[^"'`)\s\\]+/g) || [];
  return [...new Set(encontrados)];
}

/**
 * Guarda lo que se pueda y devuelve qué faltó.
 *
 * Una por una y no con `addAll`, que es atómico: un solo chunk que falle
 * dejaría la caja sin NINGÚN recurso guardado.
 */
async function guardarTodo(cache, urls) {
  const faltan = [];
  await Promise.all(
    urls.map(async (url) => {
      try {
        if (await cache.match(url)) return;
        await cache.add(new Request(url, { cache: "reload" }));
      } catch {
        faltan.push(url);
      }
    }),
  );
  return faltan;
}

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

// Para el test: se lee este archivo y se ejercen ESTAS funciones, más los
// propios listeners de `install` y `activate`.
self.__decidirRuta = decidirRuta;
self.__estaticosDelHtml = estaticosDelHtml;

/**
 * Guardar el documento NO alcanza para arrancar sin red.
 *
 * Los chunks de JavaScript y CSS que la página ya tenía cargados se pidieron
 * ANTES de que este service worker existiera, así que no pasaron por él y no
 * están en la caché. Tras un reinicio sin red, el documento se servía y sus
 * `<script>` daban error: una pantalla en blanco, que es peor que no haber
 * instalado nada. Así que en la instalación se leen del propio HTML y se
 * guardan. Los chunks que Next carga dinámicamente no figuran ahí: esos los
 * declara la página con el mensaje `PREPARAR`.
 */
async function precargar() {
  const cache = await caches.open(CACHE);
  let html = null;
  try {
    const respuesta = await fetch(DOCUMENTO, { cache: "reload" });
    if (!respuesta.ok) return;
    html = await respuesta.clone().text();
    await cache.put(DOCUMENTO, respuesta);
  } catch {
    // Sin red al instalar: se guarda en la primera visita con conexión.
    return;
  }
  await guardarTodo(cache, estaticosDelHtml(html));
}

self.addEventListener("install", (evento) => {
  evento.waitUntil(precargar());
});

self.addEventListener("activate", (evento) => {
  evento.waitUntil(
    (async () => {
      for (const nombre of await caches.keys()) {
        // SÓLO las de la caja. Con `nombre !== CACHE` se borraba cualquier
        // caché del origen, incluidas las que no son nuestras: el service
        // worker de un bloque se llevaba puesto el almacenamiento de otro.
        if (nombre !== CACHE && nombre.startsWith(PREFIJO)) await caches.delete(nombre);
      }
      await self.clients.claim();
    })(),
  );
});

self.addEventListener("message", (evento) => {
  const datos = evento.data;
  if (!datos) return;
  // La página decide cuándo es seguro activar la versión nueva.
  if (datos.type === "SKIP_WAITING") {
    self.skipWaiting();
    return;
  }
  // La página declara los recursos que realmente cargó y pregunta si quedó
  // lista. Es lo que hace que la casilla no pueda decir "PC preparada" sobre una
  // caché que no alcanza para abrir.
  if (datos.type === "PREPARAR") {
    const puerto = evento.ports && evento.ports[0];
    evento.waitUntil(
      (async () => {
        const cache = await caches.open(CACHE);
        if (!(await cache.match(DOCUMENTO))) await precargar();
        const faltan = await guardarTodo(cache, datos.urls || []);
        const documento = Boolean(await cache.match(DOCUMENTO));
        if (puerto) puerto.postMessage({ documento, faltan, lista: documento && !faltan.length });
      })(),
    );
  }
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
