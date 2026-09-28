/**
 * Qué intercepta el service worker de la caja — leyendo `public/sw.js`.
 *
 * El test ejerce la función REAL del archivo que se despliega, no una copia:
 * un service worker que cachea de más sirve chunks viejos a toda la app, y esa
 * es la peor falla posible de este bloque. Una segunda implementación "para
 * testear" podría separarse justo en el caso que importa.
 */
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";

type Decision = "ignorar" | "estatico" | "documento";
type Decidir = (
  url: URL,
  origenPropio: string,
  esNavegacion: boolean,
  aceptaRsc: boolean,
) => Decision;

const ORIGEN = "https://vektor.app";

interface CacheFalsa {
  guardados: Map<string, string>;
  match: (r: unknown) => Promise<string | undefined>;
  add: (r: unknown) => Promise<void>;
  put: (r: unknown, v: unknown) => Promise<void>;
}

interface Mundo {
  self: Record<string, any>;
  listeners: Record<string, (e: any) => void>;
  cachesAbiertas: Map<string, CacheFalsa>;
  borradas: string[];
  nombresExistentes: string[];
  respuestas: Map<string, { ok: boolean; texto: string }>;
  fallan: Set<string>;
}

function clave(r: unknown): string {
  return typeof r === "string" ? r : String((r as { url: string }).url);
}

/** Corre `public/sw.js` con un mundo falso y devuelve sus listeners reales. */
function cargar(): Mundo {
  const archivo = path.resolve(__dirname, "../../../../public/sw.js");
  const fuente = fs.readFileSync(archivo, "utf-8");
  const listeners: Record<string, (e: any) => void> = {};
  const cachesAbiertas = new Map<string, CacheFalsa>();
  const borradas: string[] = [];
  const mundo: Mundo = {
    self: {},
    listeners,
    cachesAbiertas,
    borradas,
    nombresExistentes: [],
    respuestas: new Map(),
    fallan: new Set(),
  };
  const nuevaCache = (): CacheFalsa => {
    const guardados = new Map<string, string>();
    return {
      guardados,
      match: async (r) => guardados.get(clave(r)),
      add: async (r) => {
        const url = clave(r);
        if (mundo.fallan.has(url)) throw new Error("404");
        guardados.set(url, "contenido");
      },
      put: async (r, v) => {
        guardados.set(clave(r), String(v));
      },
    };
  };
  const self: Record<string, any> = {
    addEventListener: (nombre: string, fn: (e: any) => void) => {
      listeners[nombre] = fn;
    },
    location: { origin: ORIGEN },
    clients: { claim: () => Promise.resolve() },
    skipWaiting: () => {
      self.__salteado = true;
    },
  };
  mundo.self = self;
  const caches = {
    open: async (nombre: string) => {
      if (!cachesAbiertas.has(nombre)) cachesAbiertas.set(nombre, nuevaCache());
      return cachesAbiertas.get(nombre)!;
    },
    keys: async () => mundo.nombresExistentes,
    delete: async (nombre: string) => {
      borradas.push(nombre);
      return true;
    },
  };
  const fetch = async (r: unknown) => {
    const url = clave(r);
    const guardada = mundo.respuestas.get(url);
    if (!guardada) throw new Error("sin red");
    return {
      ok: guardada.ok,
      clone: () => ({ text: async () => guardada.texto }),
      text: async () => guardada.texto,
    };
  };
  // `Request` tiene que conservar `.url`: el service worker guarda con
  // `new Request(url, {cache:"reload"})` y busca con la url pelada.
  class RequestFalsa {
    url: string;
    constructor(url: string) {
      this.url = url;
    }
  }
  vm.runInNewContext(fuente, {
    self,
    caches,
    fetch,
    URL,
    Request: RequestFalsa,
    Promise,
    Set,
    Boolean,
  });
  return mundo;
}

function cargarDecidirRuta(): Decidir {
  return cargar().self.__decidirRuta as Decidir;
}

const decidir = cargarDecidirRuta();

function ruta(u: string, opts: { navegacion?: boolean; rsc?: boolean } = {}): Decision {
  return decidir(new URL(u, ORIGEN), ORIGEN, opts.navegacion ?? false, opts.rsc ?? false);
}

describe("lo que el service worker NO toca", () => {
  it("la API del negocio", () => {
    // Una respuesta vieja del servidor es peor que un error de red: parece buena.
    expect(ruta("/api/v1/pos/operations")).toBe("ignorar");
    expect(ruta("/api/v1/pos/catalog")).toBe("ignorar");
  });

  it("la API en OTRO origen", () => {
    expect(decidir(new URL("https://api.vektor.app/v1/pos/catalog"), ORIGEN, false, false)).toBe(
      "ignorar",
    );
  });

  it("los payloads de RSC, por header o por query", () => {
    // Servir uno viejo mezcla dos versiones de la app en la misma pantalla.
    expect(ruta("/pos", { navegacion: true, rsc: true })).toBe("ignorar");
    expect(ruta("/pos?_rsc=abc", { navegacion: true })).toBe("ignorar");
  });

  it("cualquier otra ruta de la app", () => {
    expect(ruta("/dashboard", { navegacion: true })).toBe("ignorar");
    expect(ruta("/products", { navegacion: true })).toBe("ignorar");
    expect(ruta("/")).toBe("ignorar");
  });

  it("un recurso estático que NO es un chunk con hash", () => {
    expect(ruta("/screenshots/caja.png")).toBe("ignorar");
  });
});

describe("lo que sí", () => {
  it("la navegación a la caja", () => {
    expect(ruta("/pos", { navegacion: true })).toBe("documento");
    expect(ruta("/pos/algo", { navegacion: true })).toBe("documento");
  });

  it("los chunks de Next, que llevan hash en el nombre", () => {
    expect(ruta("/_next/static/chunks/main-abc123.js")).toBe("estatico");
    expect(ruta("/_next/static/css/app-xyz.css")).toBe("estatico");
  });

  it("pedir /pos sin ser navegación no es el documento", () => {
    // Un `fetch("/pos")` desde código no puede recibir el HTML cacheado.
    expect(ruta("/pos")).toBe("ignorar");
  });
});

describe("la instalación deja la PC realmente lista", () => {
  const HTML = [
    "<!doctype html><html><head>",
    '<link rel="stylesheet" href="/_next/static/css/app.css"/>',
    '<script src="/_next/static/chunks/main-abc.js"></script>',
    // Next escapa las rutas dentro de los payloads de flight: sin desescapar,
    // el primer arranque se queda sin la mitad de los chunks.
    '<script>self.__next_f=[[1,"\\"/_next/static/chunks/page-def.js\\""]]</script>',
    "</head><body></body></html>",
  ].join("");

  async function instalar(mundo: ReturnType<typeof cargar>) {
    const esperas: Promise<unknown>[] = [];
    mundo.listeners.install!({ waitUntil: (p: Promise<unknown>) => esperas.push(p) });
    await Promise.all(esperas);
    return mundo.cachesAbiertas.get("vektor-pos-v1")!;
  }

  it("del HTML saca los chunks, escapados incluidos", () => {
    const mundo = cargar();
    const urls = mundo.self.__estaticosDelHtml(HTML) as string[];
    expect(urls).toEqual(
      expect.arrayContaining([
        "/_next/static/css/app.css",
        "/_next/static/chunks/main-abc.js",
        "/_next/static/chunks/page-def.js",
      ]),
    );
  });

  it("guarda el documento Y sus estáticos, no sólo el HTML", async () => {
    // Con sólo el documento, un reinicio sin red servía la página y sus
    // `<script>` fallaban: pantalla en blanco, peor que no instalar nada. Los
    // chunks ya cargados NO pasan por el service worker, así que nunca se
    // cachean solos.
    const mundo = cargar();
    mundo.respuestas.set("/pos", { ok: true, texto: HTML });
    const cache = await instalar(mundo);
    expect([...cache.guardados.keys()]).toEqual(
      expect.arrayContaining([
        "/pos",
        "/_next/static/css/app.css",
        "/_next/static/chunks/main-abc.js",
        "/_next/static/chunks/page-def.js",
      ]),
    );
  });

  it("un chunk que falla no impide guardar los demás", async () => {
    const mundo = cargar();
    mundo.respuestas.set("/pos", { ok: true, texto: HTML });
    mundo.fallan.add("/_next/static/chunks/page-def.js");
    const cache = await instalar(mundo);
    expect(cache.guardados.has("/_next/static/chunks/main-abc.js")).toBe(true);
    expect(cache.guardados.has("/_next/static/chunks/page-def.js")).toBe(false);
  });

  it("sin red al instalar no revienta: se guarda en la primera visita con conexión", async () => {
    const mundo = cargar();
    const cache = await instalar(mundo);
    expect(cache.guardados.size).toBe(0);
  });

  it("PREPARAR contesta que NO está lista si falta un recurso declarado", async () => {
    const mundo = cargar();
    mundo.respuestas.set("/pos", { ok: true, texto: HTML });
    mundo.fallan.add("/_next/static/chunks/dinamico.js");
    const esperas: Promise<unknown>[] = [];
    const respuestas: any[] = [];
    mundo.listeners.message!({
      data: { type: "PREPARAR", urls: ["/_next/static/chunks/dinamico.js"] },
      ports: [{ postMessage: (m: unknown) => respuestas.push(m) }],
      waitUntil: (p: Promise<unknown>) => esperas.push(p),
    });
    await Promise.all(esperas);
    expect(respuestas[0]).toMatchObject({
      documento: true,
      lista: false,
      faltan: ["/_next/static/chunks/dinamico.js"],
    });
  });

  it("PREPARAR contesta lista cuando el documento y los recursos están", async () => {
    const mundo = cargar();
    mundo.respuestas.set("/pos", { ok: true, texto: HTML });
    const esperas: Promise<unknown>[] = [];
    const respuestas: any[] = [];
    mundo.listeners.message!({
      data: { type: "PREPARAR", urls: ["/_next/static/chunks/dinamico.js"] },
      ports: [{ postMessage: (m: unknown) => respuestas.push(m) }],
      waitUntil: (p: Promise<unknown>) => esperas.push(p),
    });
    await Promise.all(esperas);
    expect(respuestas[0]).toMatchObject({ documento: true, lista: true, faltan: [] });
  });
});

describe("activar no se lleva puesto lo ajeno", () => {
  it("borra sólo las cachés de la caja", async () => {
    // Antes era `nombre !== CACHE`, o sea TODA caché del origen: el service
    // worker de un bloque borraba el almacenamiento de otro.
    const mundo = cargar();
    mundo.nombresExistentes = [
      "vektor-pos-v1",
      "vektor-pos-v0",
      "workbox-precache-otra-cosa",
      "alguna-caja-ajena",
    ];
    const esperas: Promise<unknown>[] = [];
    mundo.listeners.activate!({ waitUntil: (p: Promise<unknown>) => esperas.push(p) });
    await Promise.all(esperas);
    expect(mundo.borradas).toEqual(["vektor-pos-v0"]);
  });
});
