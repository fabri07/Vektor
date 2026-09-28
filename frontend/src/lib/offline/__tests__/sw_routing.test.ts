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

function cargarDecidirRuta(): Decidir {
  const archivo = path.resolve(__dirname, "../../../../public/sw.js");
  const fuente = fs.readFileSync(archivo, "utf-8");
  const self: Record<string, unknown> = {
    addEventListener: () => {},
    location: { origin: ORIGEN },
    clients: { claim: () => Promise.resolve() },
  };
  vm.runInNewContext(fuente, { self, caches: {}, fetch: () => {}, URL });
  return self.__decidirRuta as Decidir;
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
