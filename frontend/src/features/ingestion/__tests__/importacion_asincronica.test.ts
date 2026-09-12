/**
 * E6c-3 — lo que el cliente NO puede hacer, que es donde está el riesgo.
 *
 * El defecto que estos tests existen para impedir: ante un timeout del POST, caer
 * a `/confirm`. Un timeout no dice que la petición no llegó — dice que no sabemos
 * si llegó. Si el backend la registró y además se dispara el confirm sincrónico,
 * el archivo se importa dos veces.
 *
 * La distinción que decide todo es entre **"el servidor dijo que no"** (404: la
 * ruta no está habilitada, no miró el cuerpo, no hay nada registrado) y **"no
 * sabemos qué dijo el servidor"** (timeout, red, 5xx). Sólo la primera permite
 * cambiar de ruta.
 */

import {
  esTerminal,
  importacionEnCurso,
  olvidarImportacion,
  permiteCaerAlConfirmSincronico,
  proximaEspera,
  recordarIntento,
  reservarClave,
  textoDeEstado,
} from "../importacionAsincronica";

describe("cuándo se puede cambiar de ruta", () => {
  it("un 404 sí: el servidor respondió sin mirar el cuerpo", () => {
    expect(permiteCaerAlConfirmSincronico({ response: { status: 404 } })).toBe(true);
  });

  it.each([
    ["timeout sin respuesta", {}],
    ["error de red", { message: "Network Error" }],
    ["500 del servidor", { response: { status: 500 } }],
    ["502 del proxy", { response: { status: 502 } }],
    ["504 del proxy", { response: { status: 504 } }],
    ["409 de conflicto", { response: { status: 409 } }],
  ])("%s NO: la petición pudo haberse registrado", (_caso, error) => {
    expect(permiteCaerAlConfirmSincronico(error)).toBe(false);
  });
});

describe("la clave de petición", () => {
  beforeEach(() => window.localStorage.clear());

  it("se reserva ANTES de mandar, así que sobrevive a un reload", () => {
    const { requestKey } = reservarClave("archivo-1");
    expect(requestKey).toBeTruthy();
    // Simula el reload: el módulo vuelve a leer de localStorage.
    expect(importacionEnCurso("archivo-1")?.requestKey).toBe(requestKey);
  });

  it("un reintento del mismo archivo usa la MISMA clave", () => {
    const primera = reservarClave("archivo-1").requestKey;
    const segunda = reservarClave("archivo-1").requestKey;
    expect(segunda).toBe(primera);
  });

  it("archivos distintos tienen claves distintas", () => {
    expect(reservarClave("archivo-1").requestKey).not.toBe(
      reservarClave("archivo-2").requestKey,
    );
  });

  it("olvidar una importación libera su clave", () => {
    const primera = reservarClave("archivo-1").requestKey;
    olvidarImportacion("archivo-1");
    expect(importacionEnCurso("archivo-1")).toBeNull();
    expect(reservarClave("archivo-1").requestKey).not.toBe(primera);
  });
});

describe("recuperación al recargar", () => {
  beforeEach(() => window.localStorage.clear());

  it("el id del intento queda guardado para poder retomarlo", () => {
    reservarClave("archivo-1");
    recordarIntento("archivo-1", "intento-abc");
    expect(importacionEnCurso("archivo-1")?.attemptId).toBe("intento-abc");
  });

  it("recordar un intento de un archivo que no se reservó no inventa una entrada", () => {
    recordarIntento("archivo-fantasma", "intento-abc");
    expect(importacionEnCurso("archivo-fantasma")).toBeNull();
  });

  it("un localStorage roto no rompe la pantalla", () => {
    const original = window.localStorage.getItem;
    // Modo privado, cuota llena, permisos: perder la recuperación es malo, pero
    // romper la pantalla de importación es peor.
    window.localStorage.getItem = () => {
      throw new Error("SecurityError");
    };
    expect(() => importacionEnCurso("archivo-1")).not.toThrow();
    expect(importacionEnCurso("archivo-1")).toBeNull();
    window.localStorage.getItem = original;
  });
});

describe("el ritmo de las consultas", () => {
  it("arranca rápido, para no hacer esperar a un archivo chico", () => {
    expect(proximaEspera(0)).toBeLessThanOrEqual(1000);
  });

  it("se separa a medida que la importación se alarga", () => {
    expect(proximaEspera(5)).toBeGreaterThan(proximaEspera(1));
  });

  it("tiene tope: una pestaña olvidada no consulta para siempre cada segundo", () => {
    expect(proximaEspera(100)).toBeLessThanOrEqual(5000);
  });
});

describe("estados", () => {
  it("los tres finales terminan el seguimiento", () => {
    expect(esTerminal("COMPLETADO")).toBe(true);
    expect(esTerminal("FALLADO")).toBe(true);
    expect(esTerminal("CANCELADO")).toBe(true);
  });

  it("los dos en curso no", () => {
    expect(esTerminal("PENDIENTE")).toBe(false);
    expect(esTerminal("EJECUTANDO")).toBe(false);
  });

  it("el texto dice qué está pasando, no en qué fase interna anda el ejecutor", () => {
    expect(textoDeEstado("PENDIENTE")).toMatch(/cola/i);
    expect(textoDeEstado("EJECUTANDO", "reclamado")).toBe("Importando…");
    expect(textoDeEstado("EJECUTANDO", "importando ventas")).toMatch(/importando ventas/i);
    expect(textoDeEstado("FALLADO")).toMatch(/no se pudo/i);
  });
});
