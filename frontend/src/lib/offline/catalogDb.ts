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

import { podarAprendizajesSubidos, superposicion } from "@/lib/offline/aprendizajes";
import { abrirPosDb, unidadesComprometidas } from "@/lib/offline/posDb";
import type { PosProduct } from "@/lib/pos/cart";

/**
 * Las claves de meta llevan el tenant adentro.
 *
 * Con una clave global, una PC usada por dos negocios —el mismo dueño con dos
 * locales— se quedaba con el nombre del último que sincronizó, y el ticket que
 * la caja imprime sin red salía con el nombre del OTRO negocio. La fecha de
 * última sincronización tenía el mismo problema: decía "hace 2 minutos" sobre
 * un catálogo que no era el que se estaba mostrando.
 */
const CLAVE_SYNC = (tenantId: string) => `catalogo:ultimaSync:${tenantId}`;
const CLAVE_NEGOCIO = (tenantId: string) => `catalogo:businessName:${tenantId}`;

/** Tope de páginas: 200 × 500 productos = 100.000, holgado para un kiosco. */
const MAX_PAGINAS = 200;

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

export async function ultimaSincronizacion(tenantId: string): Promise<string | null> {
  return leerMeta<string>(CLAVE_SYNC(tenantId));
}

export async function nombreDelNegocio(tenantId: string): Promise<string | null> {
  return leerMeta<string>(CLAVE_NEGOCIO(tenantId));
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
  let completo = false;
  // Tope de seguridad: un cursor que no avanza no puede colgar la caja.
  for (let pagina = 0; pagina < MAX_PAGINAS; pagina += 1) {
    const datos: PaginaDeCatalogo = await traerPagina(cursor);
    productos.push(...datos.items);
    if (datos.business_name) negocio = datos.business_name;
    if (!datos.next_cursor) {
      completo = true;
      break;
    }
    cursor = datos.next_cursor;
  }
  // Llegar al tope con cursor pendiente es lo MISMO que una página que falla:
  // no se vio el catálogo entero. Reemplazarlo igual borraría como "bajas" a
  // todos los productos que quedaron del otro lado del tope, y el escáner
  // dejaría de encontrarlos. Mejor el catálogo viejo completo que uno nuevo
  // truncado.
  if (!completo) {
    throw new Error(
      `El catálogo no terminó de bajar: más de ${MAX_PAGINAS} páginas. No se reemplazó nada.`,
    );
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

  // Un código aprendido sin red y ya subido sobra cuando el catálogo bajado lo
  // trae: desde acá lo resuelve el catálogo mismo.
  await podarAprendizajesSubidos(tenantId, new Map(productos.map((p) => [p.id, p.barcode])));

  await guardarMeta(CLAVE_SYNC(tenantId), new Date().toISOString());
  if (negocio) await guardarMeta(CLAVE_NEGOCIO(tenantId), negocio);
  return productos.length;
}

/** El catálogo guardado, con el stock estimado ya derivado. */
export async function catalogoDeCaja(tenantId: string): Promise<ProductoDeCaja[]> {
  const base = await abrirPosDb();
  const filas = await base.getAllFromIndex("catalogo", "porTenant", tenantId);
  const comprometidas = await unidadesComprometidas(tenantId);
  // Los códigos aprendidos sin red, superpuestos: el segundo escaneo los
  // encuentra igual que si el catálogo ya los trajera. Sólo sobre un producto
  // sin código: el aprendizaje nunca pisa uno (la misma regla del servidor).
  const aprendidos = await superposicion(tenantId);
  return filas.map((fila) => {
    const producto = fila.datos as unknown as PosProduct;
    const aprendido = aprendidos.get(producto.id);
    return {
      ...producto,
      barcode: producto.barcode || aprendido || producto.barcode,
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
