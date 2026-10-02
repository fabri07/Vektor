/**
 * Aprender un código SIN RED (cierre de B4).
 *
 * Con conexión, "este código es este producto" va directo a
 * `POST /products/{id}/barcode`. Sin conexión, el escaneo de un código
 * desconocido dejaba al cajero sin salida. Ahora el aprendizaje se GUARDA en la
 * PC, se superpone al catálogo local —el segundo escaneo ya lo encuentra— y se
 * sube cuando vuelve la red.
 *
 * Tres reglas:
 * - **Las mismas precondiciones que el servidor, antes de guardar**: el código
 *   tiene que ser aprendible (GTIN válido), no responder ya a otro producto, y
 *   el producto no puede tener otro código. Si no, se rechaza en el acto.
 * - **Un conflicto al subir NO toca ninguna venta.** Si otro aprendió ese código
 *   para otro producto mientras tanto (`BARCODE_TAKEN`), el aprendizaje pasa a
 *   revisión y deja de superponerse; lo vendido con él ya se vendió con el
 *   producto que el cajero eligió, que es lo que se cobró.
 * - **La superposición es DERIVADA**, como el stock estimado: vive en su propio
 *   store y `catalogoDeCaja` la aplica al leer. Reemplazar el catálogo no la
 *   borra; recién cuando el catálogo bajado ya trae el código, sobra.
 */

import {
  type AprendizajeGuardado,
  abrirPosDb,
} from "@/lib/offline/posDb";

export function claveDeAprendizaje(tenantId: string, codigo: string): string {
  return `${tenantId}:${codigo}`;
}

export interface NuevoAprendizaje {
  tenantId: string;
  userId: string;
  terminalId: string | null;
  codigo: string;
  productId: string;
  productName: string;
}

export async function guardarAprendizaje(
  nuevo: NuevoAprendizaje,
  ahora: number = Date.now(),
): Promise<AprendizajeGuardado> {
  const guardado: AprendizajeGuardado = {
    ...nuevo,
    id: claveDeAprendizaje(nuevo.tenantId, nuevo.codigo),
    estado: "PENDIENTE",
    creadoEn: new Date(ahora).toISOString(),
    intentos: 0,
    ultimoError: null,
    ultimoCodigo: null,
    proximoIntentoEn: 0,
  };
  await (await abrirPosDb()).put("aprendizajes", guardado);
  return guardado;
}

export async function listarAprendizajes(tenantId: string): Promise<AprendizajeGuardado[]> {
  const base = await abrirPosDb();
  const todos = await base.getAllFromIndex("aprendizajes", "porTenant", tenantId);
  return todos.sort((a, b) => a.creadoEn.localeCompare(b.creadoEn));
}

/** Los que ESTA sesión puede subir: misma regla de pertenencia que las ventas. */
export async function aprendizajesSincronizables(
  tenantId: string,
  userId: string,
  terminalId: string | null,
  ahora: number = Date.now(),
): Promise<AprendizajeGuardado[]> {
  return (await listarAprendizajes(tenantId)).filter(
    (a) =>
      a.estado === "PENDIENTE" &&
      a.userId === userId &&
      a.terminalId === terminalId &&
      a.proximoIntentoEn <= ahora,
  );
}

export async function actualizarAprendizaje(
  id: string,
  cambios: Partial<AprendizajeGuardado>,
): Promise<void> {
  const base = await abrirPosDb();
  const tx = base.transaction("aprendizajes", "readwrite");
  const actual = await tx.store.get(id);
  if (actual !== undefined) await tx.store.put({ ...actual, ...cambios });
  await tx.done;
}

/** Descarte MANUAL de uno en revisión. */
export async function borrarAprendizaje(id: string): Promise<void> {
  await (await abrirPosDb()).delete("aprendizajes", id);
}

/**
 * Lo que se superpone al catálogo: código → producto, de los pendientes y de los
 * ya subidos que el catálogo local todavía no trae. Uno en revisión NO: el
 * servidor dijo que ese código no es de ese producto.
 */
export async function superposicion(tenantId: string): Promise<Map<string, string>> {
  const mapa = new Map<string, string>();
  for (const a of await listarAprendizajes(tenantId)) {
    if (a.estado !== "REQUIERE_REVISION") mapa.set(a.productId, a.codigo);
  }
  return mapa;
}

/**
 * Después de reemplazar el catálogo: los ya subidos cuyo producto bajó CON ese
 * código sobran. Los pendientes nunca se tocan acá.
 */
export async function podarAprendizajesSubidos(
  tenantId: string,
  barcodePorProducto: Map<string, string | null | undefined>,
): Promise<void> {
  const base = await abrirPosDb();
  const tx = base.transaction("aprendizajes", "readwrite");
  for (const a of await tx.store.index("porTenant").getAll(tenantId)) {
    if (a.estado === "SINCRONIZADO" && barcodePorProducto.get(a.productId) === a.codigo) {
      await tx.store.delete(a.id);
    }
  }
  await tx.done;
}
