import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

export interface TableColumn<T = Record<string, unknown>> {
  key: string;
  header: string;
  render?: (value: unknown, row: T) => ReactNode;
  /** Fija la columna a la derecha para que el scroll horizontal no la tape. */
  stickyRight?: boolean;
}

interface TableProps<T = Record<string, unknown>> {
  columns: TableColumn<T>[];
  data: T[];
  emptyMessage?: string;
  /** Qué contiene la tabla, para el lector de pantalla del área desplazable. */
  ariaLabel?: string;
}

/**
 * Tabla con scroll horizontal **alcanzable**.
 *
 * Medido en el navegador antes de tocar nada (viewport 1440×900, 50 filas, las 9
 * columnas de /sales): el contenedor SÍ podía scrollear —`scrollWidth` 1328 vs
 * `clientWidth` 1200— y la barra existía, ocupando sus 15px de layout. El
 * problema era dónde: la barra vive al fondo del contenedor que scrollea, que
 * con 50 filas mide 2055px de alto, o sea **1375px por debajo del pliegue**.
 * Para usarla había que scrollear la página entera hasta el final de la tabla. Y
 * el contenedor no era focuseable, así que las flechas del teclado tampoco
 * hacían nada. Desde afuera se ve igual que "no se puede scrollear".
 *
 * (La primera hipótesis —`min-width:auto` de flex item— la refutó esa misma
 * medición: el bloque se quedaba en 1200px con y sin `min-w-0`.)
 *
 * Lo que se hace acá:
 * - El área es **focuseable** (`tabIndex=0` + `role="region"`), así que las
 *   flechas la recorren y el foco se ve.
 * - Los gradientes laterales pasan a **indicar de verdad**: antes estaban
 *   siempre encendidos, o sea que decoraban y encima tapaban la última columna
 *   simulando un borde. Ahora aparecen sólo del lado donde queda contenido.
 * - La columna de acciones queda **fija a la derecha**: era la que se cortaba, y
 *   es la que tiene los botones.
 */
export function Table<T extends object>({
  columns,
  data,
  emptyMessage = "No hay datos para mostrar.",
  ariaLabel = "Tabla de datos",
}: TableProps<T>) {
  const scrollerRef = useRef<HTMLDivElement>(null);
  const [overflow, setOverflow] = useState({ left: false, right: false });

  const medir = useCallback(() => {
    const el = scrollerRef.current;
    if (!el) return;
    const max = el.scrollWidth - el.clientWidth;
    // Tolerancia de 1px: los anchos fraccionarios de subpíxel dejaban el
    // gradiente derecho encendido en tablas que entran justo.
    setOverflow({ left: el.scrollLeft > 1, right: max > 1 && el.scrollLeft < max - 1 });
  }, []);

  useEffect(() => {
    medir();
    const el = scrollerRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    // No alcanza con medir al montar: la ventana cambia de tamaño, se muestran u
    // ocultan columnas y cambian las filas — y con ellas el ancho del contenido.
    const ro = new ResizeObserver(medir);
    ro.observe(el);
    return () => ro.disconnect();
  }, [medir, data, columns]);

  const stickyCell = (col: TableColumn<T>, base: string) =>
    col.stickyRight
      ? `${base} sticky right-0 z-[1] bg-inherit before:absolute before:inset-y-0 before:left-0 before:w-px before:bg-vk-border-w before:content-['']`
      : base;

  return (
    <div className="relative">
      {overflow.left && (
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-y-0 left-0 z-10 w-6 bg-gradient-to-r from-vk-surface-w to-transparent"
        />
      )}
      {overflow.right && (
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-y-0 right-0 z-10 w-6 bg-gradient-to-l from-vk-surface-w to-transparent"
        />
      )}

      <div
        ref={scrollerRef}
        onScroll={medir}
        // Focuseable a propósito: sin esto las flechas del teclado no llegan al
        // área desplazable y la única forma de moverse es una barra que está
        // fuera de pantalla.
        tabIndex={0}
        role="region"
        aria-label={ariaLabel}
        className="overflow-x-auto rounded-lg focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-vk-blue"
      >
        <table className="w-full min-w-full border-collapse text-sm">
          <thead>
            <tr className="border-b border-vk-border-w bg-vk-surface-w">
              {columns.map((col) => (
                <th
                  key={col.key}
                  scope="col"
                  className={stickyCell(
                    col,
                    "whitespace-nowrap px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-vk-text-secondary",
                  )}
                >
                  {col.header}
                </th>
              ))}
            </tr>
          </thead>

          <tbody>
            {data.length === 0 ? (
              <tr>
                <td
                  colSpan={columns.length}
                  className="px-4 py-8 text-center text-sm text-vk-text-muted"
                >
                  {emptyMessage}
                </td>
              </tr>
            ) : (
              data.map((row, rowIdx) => (
                <tr
                  key={rowIdx}
                  // `bg-vk-surface-w` explícito (y no heredado) porque la celda
                  // fija usa `bg-inherit`: sin fondo propio, el contenido que
                  // pasa por debajo se vería a través de la columna de acciones.
                  className="border-b border-vk-border-w/60 bg-vk-surface-w transition-colors hover:bg-vk-bg-light"
                >
                  {columns.map((col) => (
                    <td
                      key={col.key}
                      className={stickyCell(
                        col,
                        "whitespace-nowrap px-4 py-3 text-vk-text-primary",
                      )}
                    >
                      {col.render
                        ? col.render((row as Record<string, unknown>)[col.key], row)
                        : String((row as Record<string, unknown>)[col.key] ?? "")}
                    </td>
                  ))}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
