/**
 * Una sola caja por PC, y un solo envío a la vez.
 *
 * Dos pestañas de `/pos` abiertas comparten la misma base y la misma cola: sin
 * candado, las dos pueden intentar subir la misma venta al mismo tiempo. El
 * servidor no la duplicaría —el `client_operation_id` lo impide— pero el estado
 * local sí quedaría pisado, y el cajero vería una venta "pendiente" que ya está
 * sincronizada.
 *
 * Web Locks porque es la única API que sobrevive a que una pestaña se cierre de
 * golpe: el candado se libera solo. Un flag en `localStorage` quedaría tomado
 * para siempre si el navegador se cae.
 *
 * Sin `navigator.locks` (contexto no seguro, navegador viejo) **no bloquea**:
 * es preferible una caja que funciona con un riesgo conocido que una caja que
 * no abre. El candado de módulo sigue cubriendo el caso de una sola pestaña.
 */

const CANDADO_CAJA = "vektor-pos-caja";
const CANDADO_SYNC = "vektor-pos-sync";

function locks(): LockManager | null {
  if (typeof navigator === "undefined") return null;
  return navigator.locks ?? null;
}

export interface CandadoDeCaja {
  /** `false` = la caja ya está abierta en otra pestaña. */
  obtenido: boolean;
  soltar: () => void;
}

/**
 * Toma el candado de "esta pestaña es la caja" y lo mantiene hasta soltarlo.
 *
 * `ifAvailable`: si otra pestaña lo tiene, se responde en el acto en vez de
 * quedar esperando — el cajero tiene que ver el aviso, no una pantalla colgada.
 */
export async function tomarCandadoDeCaja(): Promise<CandadoDeCaja> {
  const manager = locks();
  if (manager === null) return { obtenido: true, soltar: () => {} };
  let soltar = () => {};
  const mientrasViva = new Promise<void>((resolve) => {
    soltar = resolve;
  });
  // La promesa de `request()` resuelve recién cuando TERMINA el callback, y el
  // callback tiene que seguir corriendo para que el candado siga tomado. Así
  // que se avisa desde adentro, apenas se concede: esperar el `request` dejaba
  // la caja colgada sin abrir nunca.
  return new Promise<CandadoDeCaja>((avisar) => {
    void manager.request(
      CANDADO_CAJA,
      { mode: "exclusive", ifAvailable: true },
      async (lock) => {
        if (lock === null) {
          avisar({ obtenido: false, soltar: () => {} });
          return;
        }
        avisar({ obtenido: true, soltar });
        await mientrasViva;
      },
    );
  });
}

/** Candado a nivel de módulo: cubre el caso de una sola pestaña sin Web Locks. */
let sincronizando = false;

/**
 * Corre `tarea` con la garantía de que no hay otro envío en curso, ni en esta
 * pestaña ni en otra. Si ya hay uno, devuelve `null` sin esperar.
 */
export async function conCandadoDeSync<T>(tarea: () => Promise<T>): Promise<T | null> {
  if (sincronizando) return null;
  sincronizando = true;
  try {
    const manager = locks();
    if (manager === null) return await tarea();
    return await manager.request(
      CANDADO_SYNC,
      { mode: "exclusive", ifAvailable: true },
      async (lock) => (lock === null ? null : await tarea()),
    );
  } finally {
    sincronizando = false;
  }
}
