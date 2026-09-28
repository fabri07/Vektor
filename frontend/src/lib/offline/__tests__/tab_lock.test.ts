/**
 * Una sola caja por PC, y un solo envío a la vez.
 */
import { conCandadoDeSync, tomarCandadoDeCaja } from "@/lib/offline/tabLock";

/** Un LockManager mínimo con la semántica de `ifAvailable` que usa la caja. */
function instalarWebLocks() {
  const tomados = new Set<string>();
  const manager = {
    async request(
      nombre: string,
      _opts: { mode: string; ifAvailable?: boolean },
      cb: (lock: unknown) => Promise<unknown>,
    ) {
      if (tomados.has(nombre)) return cb(null);
      tomados.add(nombre);
      try {
        return await cb({ name: nombre });
      } finally {
        tomados.delete(nombre);
      }
    },
  };
  Object.defineProperty(navigator, "locks", { value: manager, configurable: true });
  return () => {
    Reflect.deleteProperty(navigator as unknown as Record<string, unknown>, "locks");
  };
}

describe("candado de caja", () => {
  it("la segunda pestaña no lo obtiene y no queda esperando", async () => {
    const quitar = instalarWebLocks();
    const primera = await tomarCandadoDeCaja();
    expect(primera.obtenido).toBe(true);
    const segunda = await tomarCandadoDeCaja();
    expect(segunda.obtenido).toBe(false);
    // Al cerrarse la primera, el candado queda libre.
    primera.soltar();
    await new Promise((r) => setTimeout(r, 0));
    const tercera = await tomarCandadoDeCaja();
    expect(tercera.obtenido).toBe(true);
    tercera.soltar();
    quitar();
  });

  it("sin Web Locks NO bloquea: una caja que no abre es peor", async () => {
    const c = await tomarCandadoDeCaja();
    expect(c.obtenido).toBe(true);
  });
});

describe("candado de sincronización", () => {
  it("dos envíos simultáneos: sólo uno corre", async () => {
    const quitar = instalarWebLocks();
    let corriendo = 0;
    let maximo = 0;
    const tarea = async () => {
      corriendo += 1;
      maximo = Math.max(maximo, corriendo);
      await new Promise((r) => setTimeout(r, 5));
      corriendo -= 1;
      return "listo";
    };
    const [a, b] = await Promise.all([conCandadoDeSync(tarea), conCandadoDeSync(tarea)]);
    expect(maximo).toBe(1);
    expect([a, b].filter((r) => r === "listo")).toHaveLength(1);
    quitar();
  });
});
