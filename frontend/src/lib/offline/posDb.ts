/**
 * La caja escribe la venta ACÁ antes de intentar cobrar.
 *
 * El orden importa y es la razón de existir de este módulo: hasta B7 la caja
 * mandaba el POST y recién guardaba un resguardo si detectaba error de red
 * (`lib/pos/pendingOperation.ts`, una sola operación en `localStorage`, con la
 * escritura envuelta en un `try/catch` vacío). Si el navegador se cerraba entre
 * el envío y la respuesta, la venta no existía en ningún lado: ni en el
 * servidor que quizá la guardó, ni en la PC.
 *
 * Ahora la operación, sus pagos y su ticket provisional se escriben en **una**
 * transacción de IndexedDB **antes** del primer envío, y **si no se puede
 * escribir, no se cobra** — avisar es mejor que cobrar algo que puede
 * desaparecer.
 *
 * **Una venta cobrada no se borra jamás por una respuesta del servidor.** Sólo
 * cambia de estado. El único borrado es el descarte manual con confirmación.
 */

import { type DBSchema, type IDBPDatabase, openDB } from "idb";

import type { CartLine, PosOperationPayload, PosOperationResult } from "@/lib/pos/cart";
import type { PosReceipt } from "@/features/pos/ticketTemplate";

export type EstadoPendiente =
  | "PENDIENTE"
  | "SINCRONIZADA"
  | "REQUIERE_AUTENTICACION"
  | "REQUIERE_REVISION"
  /**
   * El servidor la rechazó por algo que el cajero puede corregir en el
   * mostrador (faltó stock, cambió un precio) y todavía no se rehízo el cobro.
   *
   * **No se borra.** Al apretar Cobrar ya se afirmó `charged_at`, o sea que la
   * plata entró; borrarla dejaba sin constancia un cobro que puede haberse
   * recibido, si el cajero se va o se cierra la pestaña antes de rehacerlo.
   * Tampoco se sincroniza: reenviarla daría el mismo rechazo. Desaparece sola
   * cuando el cobro corregido entra, y si no, queda a la vista.
   */
  | "EN_CORRECCION";

export interface OperacionGuardada {
  /** El mismo id que viaja como clave de idempotencia. */
  clientOperationId: string;
  /** A quién pertenece. Se valida al sincronizar: una venta que cobró Juan no
   *  se puede subir bajo la sesión de Ana, porque el servidor se la adjudicaría
   *  a Ana. */
  tenantId: string;
  userId: string;
  terminalId: string | null;
  /** Ver `NuevaOperacion.origen`. Se limpia cuando deja de hacer falta. */
  origen?: "b13";
  estado: EstadoPendiente;
  /** El cuerpo EXACTO que se manda, congelado al cerrar el ticket. */
  payload: PosOperationPayload;
  /** Para el stock estimado y para volver a mostrar el carrito. */
  lines: CartLine[];
  /** El ticket provisional, listo para imprimir sin red. */
  recibo: PosReceipt;
  creadaEn: string;
  intentos: number;
  ultimoError: string | null;
  ultimoCodigo: string | null;
  /** Epoch ms: hasta cuándo no vale la pena reintentar (backoff). */
  proximoIntentoEn: number;
  resultado: PosOperationResult | null;
  sincronizadaEn: string | null;
}

/**
 * Un código aprendido SIN RED (cierre de B4): "este código es este producto",
 * pendiente de subir. Ver `lib/offline/aprendizajes.ts`.
 */
export type EstadoAprendizaje = "PENDIENTE" | "SINCRONIZADO" | "REQUIERE_REVISION";

export interface AprendizajeGuardado {
  /** `${tenantId}:${codigo}`: un código se aprende una sola vez por negocio. */
  id: string;
  tenantId: string;
  userId: string;
  terminalId: string | null;
  codigo: string;
  productId: string;
  productName: string;
  estado: EstadoAprendizaje;
  creadoEn: string;
  intentos: number;
  ultimoError: string | null;
  ultimoCodigo: string | null;
  proximoIntentoEn: number;
}

export interface ProductoGuardado {
  id: string;
  tenantId: string;
  /** El producto tal como lo bajó `/pos/catalog`, con su `price_token`. */
  datos: Record<string, unknown>;
}

interface EsquemaPos extends DBSchema {
  operaciones: {
    key: string;
    value: OperacionGuardada;
    indexes: { porEstado: string; porTenant: string };
  };
  catalogo: {
    key: string;
    value: ProductoGuardado;
    indexes: { porTenant: string };
  };
  meta: { key: string; value: { clave: string; valor: unknown } };
  aprendizajes: {
    key: string;
    value: AprendizajeGuardado;
    indexes: { porTenant: string };
  };
}

const NOMBRE = "vektor-pos";
/** v2 (cierre de B4): store `aprendizajes`. Subir de versión no toca los demás
 *  stores: `upgrade` sólo crea lo que falta. */
const VERSION = 2;

let db: Promise<IDBPDatabase<EsquemaPos>> | null = null;

export function abrirPosDb(): Promise<IDBPDatabase<EsquemaPos>> {
  if (db === null) {
    db = openDB<EsquemaPos>(NOMBRE, VERSION, {
      upgrade(base) {
        if (!base.objectStoreNames.contains("operaciones")) {
          const ops = base.createObjectStore("operaciones", {
            keyPath: "clientOperationId",
          });
          ops.createIndex("porEstado", "estado");
          ops.createIndex("porTenant", "tenantId");
        }
        if (!base.objectStoreNames.contains("catalogo")) {
          const cat = base.createObjectStore("catalogo", { keyPath: "id" });
          cat.createIndex("porTenant", "tenantId");
        }
        if (!base.objectStoreNames.contains("meta")) {
          base.createObjectStore("meta", { keyPath: "clave" });
        }
        if (!base.objectStoreNames.contains("aprendizajes")) {
          const apr = base.createObjectStore("aprendizajes", { keyPath: "id" });
          apr.createIndex("porTenant", "tenantId");
        }
      },
    });
  }
  return db;
}

/** Sólo para los tests: la caja nunca cierra su base. */
export function _resetPosDb(): void {
  db = null;
}

/**
 * Sólo para los tests: deja la base vacía sin cerrar la conexión.
 *
 * Borrar la base entera se bloquea mientras haya una conexión abierta, y en un
 * test eso cuelga. Vaciar los stores es equivalente y determinístico.
 */
export async function _limpiarPosDb(): Promise<void> {
  const base = await abrirPosDb();
  const tx = base.transaction(["operaciones", "catalogo", "meta", "aprendizajes"], "readwrite");
  await Promise.all([
    tx.objectStore("operaciones").clear(),
    tx.objectStore("catalogo").clear(),
    tx.objectStore("meta").clear(),
    tx.objectStore("aprendizajes").clear(),
    tx.done,
  ]);
}

/**
 * Cuánto se le reserva al primer envío antes de que la cola pueda tomarlo.
 *
 * 4× el timeout del cliente (`lib/api.ts`: 15 s), que es lo máximo que el primer
 * POST puede estar en vuelo. Suficiente para que la cola nunca se le adelante, y
 * corto para que una venta cuya pestaña murió no espere de más.
 */
export const MS_DE_PRIMER_ENVIO = 60_000;

export interface NuevaOperacion {
  clientOperationId: string;
  /**
   * `"b13"` = viene del resguardo de `localStorage` anterior a B7. Su primer
   * envío YA ocurrió y se perdió la respuesta, así que se reclama con el
   * contenido ORIGINAL antes de adaptarlo (ver `usePosSync`).
   */
  origen?: "b13";
  tenantId: string;
  userId: string;
  terminalId: string | null;
  payload: PosOperationPayload;
  lines: CartLine[];
  recibo: PosReceipt;
}

/**
 * Deja la venta guardada. Si tira, la caja NO manda el cobro.
 *
 * Una sola transacción: la operación, sus pagos (que van dentro del payload) y
 * su ticket entran juntos o no entra nada. Media venta guardada sería peor que
 * ninguna, porque al recuperarla no se sabría qué se cobró.
 */
export interface OpcionesDeGuardado {
  ahora?: number;
  /**
   * `true` cuando NO hay un primer envío por delante y la cola puede tomarla ya
   * mismo: es el caso de la venta migrada desde B13, cuyo envío original pasó
   * hace rato y se perdió la respuesta.
   */
  sinPrimerEnvio?: boolean;
}

export async function guardarOperacion(
  nueva: NuevaOperacion,
  { ahora = Date.now(), sinPrimerEnvio = false }: OpcionesDeGuardado = {},
): Promise<OperacionGuardada> {
  const guardada: OperacionGuardada = {
    ...nueva,
    estado: "PENDIENTE",
    creadaEn: new Date(ahora).toISOString(),
    intentos: 0,
    ultimoError: null,
    ultimoCodigo: null,
    // La cola no la toma hasta que el primer envío termine. Con
    // `proximoIntentoEn: 0` el motor periódico podía agarrarla **mientras el
    // primer POST seguía en vuelo** y reenviarla con `X-POS-Offline: 1`: la
    // idempotencia evita el ticket duplicado, pero si ganaba ese envío se
    // capturaba stock que el intento online habría rechazado con
    // `INSUFFICIENT_STOCK` para que el cajero decidiera.
    //
    // Es una espera y no un estado nuevo a propósito: se cura sola. Si el
    // navegador muere entre guardar y mandar, a los `MS_DE_PRIMER_ENVIO` la
    // venta vuelve a ser sincronizable sin que nadie la destrabe.
    proximoIntentoEn: sinPrimerEnvio ? 0 : ahora + MS_DE_PRIMER_ENVIO,
    resultado: null,
    sincronizadaEn: null,
  };
  const base = await abrirPosDb();
  const tx = base.transaction("operaciones", "readwrite");
  await tx.store.put(guardada);
  await tx.done;
  return guardada;
}

export async function obtenerOperacion(
  clientOperationId: string,
): Promise<OperacionGuardada | undefined> {
  return (await abrirPosDb()).get("operaciones", clientOperationId);
}

export async function actualizarOperacion(
  clientOperationId: string,
  cambios: Partial<OperacionGuardada>,
): Promise<OperacionGuardada | undefined> {
  const base = await abrirPosDb();
  const tx = base.transaction("operaciones", "readwrite");
  const actual = await tx.store.get(clientOperationId);
  if (actual === undefined) {
    await tx.done;
    return undefined;
  }
  const nueva = { ...actual, ...cambios };
  await tx.store.put(nueva);
  await tx.done;
  return nueva;
}

/**
 * Las ventas de esta PC que quedaron atadas a OTRA caja.
 *
 * Una PC rehabilitada recibe una terminal con otro id, y lo que cobró antes
 * conserva el viejo: la cola sólo sube lo de la caja actual
 * (`pendientesSincronizables`), así que esas ventas no suben solas. Como
 * IndexedDB es local, toda venta guardada acá se cobró en esta PC.
 *
 * Quedan fuera las sincronizadas (no hace falta nada) y las que están en
 * corrección (no se suben: esperan que el cajero rehaga el cobro).
 */
export function deOtraCaja(
  operaciones: OperacionGuardada[],
  terminalActual: string | null,
): OperacionGuardada[] {
  return operaciones.filter(
    (o) =>
      o.terminalId !== terminalActual &&
      o.estado !== "SINCRONIZADA" &&
      o.estado !== "EN_CORRECCION",
  );
}

/**
 * Pasa a la caja actual las ventas que cobró la anterior. Se llama SÓLO con el
 * 200 de `POST /pos/terminals/adopt-pending`: el dueño, con PIN, autorizó el
 * cambio y quedó auditado. Antes de eso no se reescribe nada.
 *
 * Una sola transacción, y vuelve a verificar cada venta: sólo se mueve la que
 * sigue siendo del mismo negocio, de la caja que se autorizó y sin sincronizar.
 */
export async function reasignarTerminal(
  tenantId: string,
  deTerminal: string | null,
  aTerminal: string,
  ids: string[],
): Promise<number> {
  const base = await abrirPosDb();
  const tx = base.transaction("operaciones", "readwrite");
  let movidas = 0;
  for (const id of ids) {
    const op = await tx.store.get(id);
    if (
      op === undefined ||
      op.tenantId !== tenantId ||
      op.terminalId !== deTerminal ||
      op.estado === "SINCRONIZADA" ||
      op.estado === "EN_CORRECCION"
    ) {
      continue;
    }
    await tx.store.put({
      ...op,
      terminalId: aTerminal,
      // La caja vieja daba TERMINAL_DISABLED: con la nueva eso ya no aplica.
      estado: op.estado === "REQUIERE_AUTENTICACION" ? "PENDIENTE" : op.estado,
      proximoIntentoEn: 0,
    });
    movidas += 1;
  }
  await tx.done;
  return movidas;
}

/** Descarte MANUAL. Nada automático llama a esto. */
export async function borrarOperacion(clientOperationId: string): Promise<void> {
  const base = await abrirPosDb();
  await base.delete("operaciones", clientOperationId);
}

export async function listarOperaciones(tenantId: string): Promise<OperacionGuardada[]> {
  const base = await abrirPosDb();
  const todas = await base.getAllFromIndex("operaciones", "porTenant", tenantId);
  return todas.sort((a, b) => a.creadaEn.localeCompare(b.creadaEn));
}

/** Las que NO están sincronizadas: lo que todavía le debe plata al negocio. */
export async function listarNoSincronizadas(
  tenantId: string,
): Promise<OperacionGuardada[]> {
  return (await listarOperaciones(tenantId)).filter((o) => o.estado !== "SINCRONIZADA");
}

/**
 * Las que ESTA sesión puede subir ahora.
 *
 * Tenant, usuario y caja tienen que coincidir. Una venta que cobró Juan espera
 * a que entre Juan: subirla bajo otra sesión se la adjudicaría a quien esté
 * logueado, y el arqueo del turno quedaría mal para los dos.
 */
export async function pendientesSincronizables(
  tenantId: string,
  userId: string,
  terminalId: string | null,
  ahora: number = Date.now(),
): Promise<OperacionGuardada[]> {
  const todas = await listarOperaciones(tenantId);
  return todas.filter(
    (o) =>
      o.estado === "PENDIENTE" &&
      o.userId === userId &&
      o.terminalId === terminalId &&
      o.proximoIntentoEn <= ahora,
  );
}

/**
 * Cuántas unidades de cada producto están comprometidas y todavía no bajaron
 * del stock del servidor.
 *
 * Es la ÚNICA fuente del stock estimado (ver `catalogDb.stockEstimado`): el
 * descuento no se acumula en el producto guardado, se DERIVA de acá. Con las
 * dos cosas a la vez, una sincronización del catálogo volvería a restar lo que
 * ya estaba restado.
 */
export async function unidadesComprometidas(
  tenantId: string,
): Promise<Map<string, number>> {
  const pendientes = await listarNoSincronizadas(tenantId);
  const porProducto = new Map<string, number>();
  for (const op of pendientes) {
    for (const item of op.payload.items) {
      porProducto.set(
        item.product_id,
        (porProducto.get(item.product_id) ?? 0) + item.quantity,
      );
    }
  }
  return porProducto;
}

/** Cuánto se conserva una venta YA subida. Ver `podarSincronizadas`. */
export const DIAS_DE_GRACIA = 7;

/**
 * Borra las ventas ya subidas de hace más de una semana.
 *
 * Una caja con 200 ventas por día acumularía 73.000 filas por año en una base
 * que el navegador puede evacuar por tamaño — y evacuarla se llevaría puestas
 * las PENDIENTES, que son las únicas que no están en ningún otro lado. Lo
 * sincronizado ya vive en el servidor: acá se conserva sólo para reimprimir el
 * comprobante de los últimos días.
 *
 * **Sólo toca `SINCRONIZADA`.** Una venta en cualquier otro estado no se borra
 * por vieja, por más años que pasen.
 */
export async function podarSincronizadas(
  tenantId: string,
  ahora: number = Date.now(),
): Promise<number> {
  const corte = ahora - DIAS_DE_GRACIA * 24 * 60 * 60 * 1000;
  const base = await abrirPosDb();
  const tx = base.transaction("operaciones", "readwrite");
  let borradas = 0;
  for (const op of await tx.store.index("porTenant").getAll(tenantId)) {
    if (op.estado !== "SINCRONIZADA") continue;
    const cuando = Date.parse(op.sincronizadaEn ?? op.creadaEn);
    if (Number.isFinite(cuando) && cuando < corte) {
      await tx.store.delete(op.clientOperationId);
      borradas += 1;
    }
  }
  await tx.done;
  return borradas;
}

/**
 * Le pide al navegador que no evacúe estos datos bajo presión de disco.
 *
 * IndexedDB sola no lo garantiza: Chrome lo concede por heurística (sitio
 * instalado, engagement). Si dice que no, la exportación de pendientes deja de
 * ser el último recurso y pasa a ser un camino de primera.
 */
export async function pedirPersistencia(): Promise<boolean> {
  try {
    if (typeof navigator === "undefined" || !navigator.storage?.persist) return false;
    if (await navigator.storage.persisted()) return true;
    return await navigator.storage.persist();
  } catch {
    return false;
  }
}
