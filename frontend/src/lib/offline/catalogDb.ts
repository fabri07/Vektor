/**
 * El catálogo que la caja tiene en la PC para poder vender sin red.
 *
 * Se baja COMPLETO (`/pos/catalog` pagina por cursor justamente para esto) y se
 * reemplaza entero: lo que no vino en la descarga se borra, y así las bajas del
 * catálogo se reflejan solas sin que el servidor tenga que llevar una lista de
 * borrados.
 *
 * **El stock estimado es DERIVADO, nunca acumulado.** Se calcula como
 * `stock del servidor − unidades de las ventas todavía no sincronizadas`. Si en
 * cambio se descontara del producto guardado al cobrar Y se volviera a restar
 * al sincronizar el catálogo, la misma venta se contaría dos veces. Una sola
 * fórmula, un solo lugar.
 */

import { abrirPosDb, unidadesComprometidas } from "@/lib/offline/posDb";
import type { PosProduct } from "@/lib/pos/cart";

const CLAVE_SYNC = "catalogo:ultimaSync";
const CLAVE_NEGOCIO = "catalogo:businessName";
const CLAVE_TENANT = "catalogo:tenantId";

export interface PaginaDeCatalogo {
  items: PosProduct[];
  next_cursor: string | null;
  business_name?: string | null;
}

export interface ProductoDeCaja extends PosProduct {
  /** Lo que la caja cree que hay. Se muestra como "estimado", nunca frena una venta. */
  stock_estimado: number;
}

async function guardarMeta(clave: string, valor: unknown): Promise<void> {
  const base = await abrirPosDb();
  await base.put("meta", { clave, valor });
}

async function leerMeta<T>(clave: string): Promise<T | null> {
  const base = await abrirPosDb();
  const fila = await base.get("meta", clave);
  return fila === undefined ? null : (fila.valor as T);
}

export async function ultimaSincronizacion(): Promise<string | null> {
  return leerMeta<string>(CLAVE_SYNC);
}

export async function nombreDelNegocio(): Promise<string | null> {
  return leerMeta<string>(CLAVE_NEGOCIO);
}

/**
 * Baja el catálogo entero y reemplaza el guardado.
 *
 * `traerPagina` se inyecta para poder probar la paginación sin red. Si una
 * página falla, NO se borra nada: mejor un catálogo viejo completo que uno
 * nuevo a medias, que dejaría productos sin poder escanear.
 */
export async function sincronizarCatalogo(
  tenantId: string,
  traerPagina: (after: string | null) => Promise<PaginaDeCatalogo>,
): Promise<number> {
  const productos: PosProduct[] = [];
  let negocio: string | null = null;
  let cursor: string | null = null;
  // Tope de seguridad: un cursor que no avanza no puede colgar la caja.
  for (let pagina = 0; pagina < 200; pagina += 1) {
    const datos: PaginaDeCatalogo = await traerPagina(cursor);
    productos.push(...datos.items);
    if (datos.business_name) negocio = datos.business_name;
    if (!datos.next_cursor) break;
    cursor = datos.next_cursor;
  }

  const base = await abrirPosDb();
  const tx = base.transaction("catalogo", "readwrite");
  const vistos = new Set(productos.map((p) => p.id));
  const anteriores = await tx.store.index("porTenant").getAllKeys(tenantId);
  for (const id of anteriores) {
    // Lo que ya no vino es una baja del catálogo.
    if (!vistos.has(String(id))) await tx.store.delete(id);
  }
  for (const p of productos) {
    await tx.store.put({ id: p.id, tenantId, datos: p as unknown as Record<string, unknown> });
  }
  await tx.done;

  await guardarMeta(CLAVE_SYNC, new Date().toISOString());
  await guardarMeta(CLAVE_TENANT, tenantId);
  if (negocio) await guardarMeta(CLAVE_NEGOCIO, negocio);
  return productos.length;
}

/** El catálogo guardado, con el stock estimado ya derivado. */
export async function catalogoDeCaja(tenantId: string): Promise<ProductoDeCaja[]> {
  const base = await abrirPosDb();
  const filas = await base.getAllFromIndex("catalogo", "porTenant", tenantId);
  const comprometidas = await unidadesComprometidas(tenantId);
  return filas.map((fila) => {
    const producto = fila.datos as unknown as PosProduct;
    return {
      ...producto,
      stock_estimado: producto.stock_units - (comprometidas.get(producto.id) ?? 0),
    };
  });
}

export async function borrarCatalogo(tenantId: string): Promise<void> {
  const base = await abrirPosDb();
  const tx = base.transaction("catalogo", "readwrite");
  for (const id of await tx.store.index("porTenant").getAllKeys(tenantId)) {
    await tx.store.delete(id);
  }
  await tx.done;
}
