"use client";

import { useEffect, type ReactNode } from "react";

export interface ProductoElegible {
  id: string;
  name: string;
  /** Primer día con una fuente con costo (compra o stock inicial); null: ninguna. */
  primer_dia_de_fuente?: string | null;
}

function fechaCorta(iso: string): string {
  const [y, m, d] = iso.slice(0, 10).split("-");
  return `${d}/${m}/${y}`;
}

/** Con `dia` (YYYY-MM-DD): ¿el producto tiene respaldo ese día? `undefined`: no se sabe. */
export function respaldoA(p: ProductoElegible, dia: string | undefined): boolean | undefined {
  if (!dia || p.primer_dia_de_fuente === undefined) return undefined;
  return !!p.primer_dia_de_fuente && p.primer_dia_de_fuente.slice(0, 10) <= dia;
}

function etiqueta(p: ProductoElegible, dia: string | undefined): string {
  if (respaldoA(p, dia) !== false) return p.name;
  return p.primer_dia_de_fuente
    ? `${p.name} · sin compra ni stock antes del ${fechaCorta(p.primer_dia_de_fuente)}`
    : `${p.name} · sin compra ni stock inicial`;
}

/**
 * «¿Qué producto se vendió?»: búsqueda + selector del catálogo. Lo usan el modal
 * de «Editar e importar» (hotfix #159) y la cola «Vincular a producto» (B·8b).
 *
 * Controlado: la búsqueda y el producto viven en quien lo usa (el modal comparte
 * la búsqueda con la compra). Si `referencia` (lo que decía el archivo) es el
 * nombre exacto de UN producto del catálogo, se preselecciona.
 */
export function ProductoVendidoSelector({
  productos,
  busqueda,
  onBusqueda,
  value,
  onChange,
  referencia,
  inputClassName,
  cargando = false,
  diaDeLaVenta,
  children,
}: {
  productos: ProductoElegible[];
  busqueda: string;
  onBusqueda: (texto: string) => void;
  value: string;
  onChange: (productId: string) => void;
  referencia: string;
  inputClassName: string;
  /** El catálogo todavía se está cargando: se dice, en vez de mostrar un combo vacío. */
  cargando?: boolean;
  /** Día de la venta (YYYY-MM-DD): ordena primero los productos con respaldo ese día y marca el
   * resto. No bloquea: el que decide es el servidor. */
  diaDeLaVenta?: string;
  /** Lo que va al lado del selector (la cantidad, en el modal). */
  children?: ReactNode;
}) {
  const nombre = referencia.trim().toLowerCase();
  useEffect(() => {
    if (value || !nombre) return;
    const exacto = productos.filter((p) => p.name.trim().toLowerCase() === nombre);
    if (exacto.length === 1) onChange(exacto[0]!.id);
  }, [value, nombre, productos, onChange]);

  const filtro = busqueda.trim().toLowerCase();
  // Lo que decía el archivo («Lote camperas») puede no estar contenido en el nombre
  // de ningún producto: filtrar igual dejaba el combo vacío y sin salida. Sin
  // coincidencias se muestra el catálogo entero y se dice por qué.
  const hayCoincidencias = !filtro || productos.some((p) => p.name.toLowerCase().includes(filtro));
  const sinCoincidencias = !cargando && productos.length > 0 && !hayCoincidencias;
  const select = (
    <select
      aria-label="Producto vendido"
      className={inputClassName}
      value={value}
      onChange={(e) => onChange(e.target.value)}
    >
      <option value="">{cargando ? "Cargando productos…" : "Elegí el producto"}</option>
      {productos
        .filter(
          (p) =>
            p.id === value || !filtro || sinCoincidencias || p.name.toLowerCase().includes(filtro),
        )
        // Sort estable: los respaldados a la fecha primero, el resto en su orden.
        .map((p, i) => ({ p, i, ok: respaldoA(p, diaDeLaVenta) === false ? 1 : 0 }))
        .sort((a, b) => a.ok - b.ok || a.i - b.i)
        .map(({ p }) => (
          <option key={p.id} value={p.id}>
            {etiqueta(p, diaDeLaVenta)}
          </option>
        ))}
    </select>
  );

  return (
    <>
      <label className="grid gap-1 text-sm text-vektor-body">
        ¿Qué producto se vendió?
        <input
          className={inputClassName}
          placeholder="Buscar producto"
          value={busqueda}
          onChange={(e) => onBusqueda(e.target.value)}
        />
      </label>
      {children ? <div className="grid grid-cols-[1fr_6rem] gap-2">{select}{children}</div> : select}
      {sinCoincidencias ? (
        <p className="text-xs text-vk-text-muted">
          Ningún producto contiene «{busqueda.trim()}»: se muestran todos. Borrá la búsqueda o
          elegí el que corresponda.
        </p>
      ) : null}
    </>
  );
}
