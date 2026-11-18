"use client";

// Combo («Jean + buzo» $41.000): una fila de «Otros» con varios productos y un solo
// monto. Véktor propone las combinaciones que suman EXACTAMENTE el monto con el precio
// de venta vigente (el servidor, `combo-suggestions`); la persona elige una o arma las
// líneas a mano. La suma de las líneas tiene que dar el monto de la fila: nada se
// reparte por ella ni se escribe con una suma que no cierra.
import { Plus, Trash2 } from "lucide-react";
import type { ComboSuggestionsResponse } from "@/services/others.service";
import { etiqueta, respaldoA, type ProductoElegible } from "./ProductoVendidoSelector";

export interface LineaDeCombo {
  productId: string;
  quantity: string;
  unitPrice: string;
}

export const MAX_LINEAS_DE_COMBO = 4;
export const LINEA_VACIA: LineaDeCombo = { productId: "", quantity: "1", unitPrice: "" };

const moneda = new Intl.NumberFormat("es-AR", { style: "currency", currency: "ARS" });

const aCentavos = (texto: string): number => Math.round(Number(texto.replace(",", ".")) * 100);

/** Σ cantidad × precio, en centavos (la cuenta exacta del servidor: `importe`). */
export function sumaEnCentavos(lineas: LineaDeCombo[]): number {
  return lineas.reduce((acc, l) => acc + Number(l.quantity) * aCentavos(l.unitPrice), 0);
}

/** `null` si el combo se puede mandar; si no, qué falta. */
export function errorDeCombo(lineas: LineaDeCombo[], montoDeLaFila: number): string | null {
  if (lineas.length < 2) return "Un combo tiene al menos dos productos.";
  const incompleta = lineas.some(
    (l) =>
      !l.productId ||
      !/^\d+$/.test(l.quantity) ||
      Number(l.quantity) < 1 ||
      !(Number(l.unitPrice.replace(",", ".")) > 0),
  );
  if (incompleta) return "Elegí el producto, la cantidad y el precio de cada línea.";
  if (new Set(lineas.map((l) => l.productId)).size !== lineas.length) {
    return "Hay un producto repetido: unificá la cantidad en una línea.";
  }
  const suma = sumaEnCentavos(lineas);
  const esperado = Math.round(montoDeLaFila * 100);
  if (suma !== esperado) {
    return `Las líneas suman ${moneda.format(suma / 100)} y la fila dice ${moneda.format(esperado / 100)}.`;
  }
  return null;
}

export function ComboVentaLineas({
  productos,
  lineas,
  onLineas,
  montoDeLaFila,
  sugerencias,
  mostrarErrores,
  onVolver,
  inputClassName,
  diaDeLaVenta,
}: {
  productos: ProductoElegible[];
  lineas: LineaDeCombo[];
  onLineas: (lineas: LineaDeCombo[]) => void;
  montoDeLaFila: number;
  sugerencias: ComboSuggestionsResponse | undefined;
  mostrarErrores: boolean;
  onVolver: () => void;
  inputClassName: string;
  /** Día de la venta (YYYY-MM-DD): los productos con respaldo ese día van primero y el resto
   * queda marcado. No bloquea: el que decide es el servidor. */
  diaDeLaVenta?: string;
}) {
  const setLinea = (i: number, patch: Partial<LineaDeCombo>) =>
    onLineas(lineas.map((l, j) => (j === i ? { ...l, ...patch } : l)));
  // Sort estable: los respaldados a la fecha primero, el resto en su orden.
  const opciones = productos
    .map((p, i) => ({ p, i, sin: respaldoA(p, diaDeLaVenta) === false ? 1 : 0 }))
    .sort((a, b) => a.sin - b.sin || a.i - b.i)
    .map(({ p }) => p);
  const error = errorDeCombo(lineas, montoDeLaFila);
  const suma = sumaEnCentavos(lineas);
  const cierra = suma === Math.round(montoDeLaFila * 100);

  return (
    <div className="grid gap-3 rounded border border-vk-border-w p-3" data-testid="combo-venta">
      <div className="flex items-center justify-between gap-2">
        <p className="text-sm font-medium text-vektor-body">Venta combo</p>
        <button type="button" onClick={onVolver} className="text-xs text-vk-blue underline">
          Volver a un solo producto
        </button>
      </div>

      {sugerencias && sugerencias.solutions.length > 0 ? (
        <div className="grid gap-1">
          <p className="text-xs text-vk-text-muted">
            Con el precio de venta de hoy, {sugerencias.solutions.length === 1 ? "esta combinación suma" : "estas combinaciones suman"}{" "}
            exactamente {sugerencias.amount ? moneda.format(Number(sugerencias.amount)) : "el monto"}
            {" "}(el precio de aquel día pudo ser otro: confirmalo):
          </p>
          {sugerencias.solutions.map((sol, i) => (
            <button
              key={sol.map((l) => `${l.product_id}x${l.quantity}`).join("+")}
              type="button"
              onClick={() =>
                onLineas(
                  sol.map((l) => ({
                    productId: l.product_id,
                    quantity: String(l.quantity),
                    unitPrice: l.unit_price,
                  })),
                )
              }
              className="rounded border border-vk-border-w px-3 py-2 text-left text-sm hover:bg-vk-surface-alt"
              aria-label={`Usar la combinación ${i + 1}`}
            >
              {sol.map((l) => `${l.quantity} × ${l.product_name}`).join(" + ")}
            </button>
          ))}
        </div>
      ) : sugerencias?.reason ? (
        <p className="text-xs text-vk-text-muted">{sugerencias.reason} Armalo a mano.</p>
      ) : null}

      {lineas.map((l, i) => (
        <div key={i} className="grid grid-cols-[1fr_5rem_7rem_auto] items-end gap-2">
          <label className="grid gap-1 text-xs text-vk-text-muted">
            Producto
            <select
              aria-label={`Producto de la línea ${i + 1}`}
              className={inputClassName}
              value={l.productId}
              onChange={(e) => setLinea(i, { productId: e.target.value })}
            >
              <option value="">Elegí un producto…</option>
              {opciones.map((p) => (
                <option key={p.id} value={p.id}>
                  {etiqueta(p, diaDeLaVenta)}
                </option>
              ))}
            </select>
          </label>
          <label className="grid gap-1 text-xs text-vk-text-muted">
            Cantidad
            <input
              aria-label={`Cantidad de la línea ${i + 1}`}
              className={inputClassName}
              type="number"
              min={1}
              step={1}
              value={l.quantity}
              onChange={(e) => setLinea(i, { quantity: e.target.value })}
            />
          </label>
          <label className="grid gap-1 text-xs text-vk-text-muted">
            Precio unitario
            <input
              aria-label={`Precio de la línea ${i + 1}`}
              className={inputClassName}
              inputMode="decimal"
              value={l.unitPrice}
              onChange={(e) => setLinea(i, { unitPrice: e.target.value })}
            />
          </label>
          <button
            type="button"
            aria-label={`Quitar la línea ${i + 1}`}
            disabled={lineas.length <= 2}
            onClick={() => onLineas(lineas.filter((_, j) => j !== i))}
            className="rounded border border-vk-border-w p-2 disabled:opacity-40"
          >
            <Trash2 size={14} />
          </button>
        </div>
      ))}

      <div className="flex items-center justify-between gap-2">
        <button
          type="button"
          disabled={lineas.length >= MAX_LINEAS_DE_COMBO}
          onClick={() => onLineas([...lineas, { ...LINEA_VACIA }])}
          className="inline-flex items-center gap-1 text-xs text-vk-blue disabled:opacity-40"
        >
          <Plus size={12} /> Agregar línea
        </button>
        <span
          className={`text-xs ${cierra ? "text-vk-text-muted" : "text-vk-danger"}`}
          data-testid="combo-suma"
        >
          Suma {moneda.format(suma / 100)} · la fila dice {moneda.format(montoDeLaFila)}
        </span>
      </div>
      {mostrarErrores && error ? (
        <p role="alert" className="text-xs text-vk-danger">
          {error}
        </p>
      ) : null}
    </div>
  );
}
