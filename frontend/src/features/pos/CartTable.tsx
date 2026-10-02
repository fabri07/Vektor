"use client";

import { Trash2 } from "lucide-react";

import { MoneyInput, QuantityInput } from "@/features/pos/DiscountControl";
import { PriceOverrideControl } from "@/features/pos/PriceOverrideControl";
import { esPorPeso, etiquetaDeUnidad } from "@/lib/pos/saleUnit";
import {
  type CartLine,
  type SaleAction,
  formatCents,
  lineNetCents,
  precioEfectivoCents,
} from "@/lib/pos/cart";

interface CartTableProps {
  lines: CartLine[];
  dispatch: (a: SaleAction) => void;
  /** El carrito sólo se toca en `editando`: en duda o enviando queda bloqueado. */
  locked: boolean;
  canDiscount: boolean;
  /** Permiso `price_override`: cobrar una línea a otro precio, con motivo. */
  canOverridePrice?: boolean;
  onAfterAction?: () => void;
}

/** La unidad en la que se TIPEA la cantidad: kg o L para un producto por peso. */
function unitLabel(line: CartLine): string {
  return etiquetaDeUnidad(line.product.sale_unit, line.product.base_units_per_sale_unit);
}

export function CartTable({
  lines,
  dispatch,
  locked,
  canDiscount,
  canOverridePrice = false,
  onAfterAction,
}: CartTableProps) {
  if (lines.length === 0) {
    return (
      <div className="flex flex-1 items-center justify-center rounded-xl border border-dashed border-pos-border p-10 text-sm text-pos-muted">
        Escaneá un producto o buscalo por nombre.
      </div>
    );
  }

  return (
    <div className="overflow-auto rounded-xl border border-pos-border bg-pos-surface">
      <table className="w-full text-sm">
        <thead className="text-left text-xs uppercase tracking-wide text-pos-muted">
          <tr>
            <th className="px-3 py-2">Producto</th>
            <th className="px-3 py-2">Cantidad</th>
            <th className="px-3 py-2 text-right">Precio</th>
            {canDiscount ? <th className="px-3 py-2 text-right">Descuento</th> : null}
            <th className="px-3 py-2 text-right">Importe</th>
            <th className="px-3 py-2" />
          </tr>
        </thead>
        <tbody>
          {lines.map((line) => {
            const id = line.product.id;
            const sinStock = line.quantity > line.product.stock_units;
            return (
              <tr key={id} className="border-t border-pos-border" data-testid="cart-line">
                <td className="px-3 py-2">
                  <p className="font-medium text-pos-text">{line.product.name}</p>
                  {sinStock ? (
                    <p className="text-xs text-pos-danger">
                      Stock disponible: {line.product.stock_display ?? line.product.stock_units}
                    </p>
                  ) : null}
                </td>
                <td className="px-3 py-2">
                  <div className="flex items-center gap-1">
                    <QuantityInput
                      label={`Cantidad de ${line.product.name}`}
                      quantity={line.quantity}
                      factor={line.product.base_units_per_sale_unit}
                      disabled={locked}
                      onQuantity={(q) => dispatch({ type: "SET_QUANTITY", productId: id, quantity: q })}
                    />
                    <span className="text-xs text-pos-muted">{unitLabel(line)}</span>
                  </div>
                </td>
                <td className="px-3 py-2 text-right text-pos-text-2">
                  {canOverridePrice ? (
                    <PriceOverrideControl line={line} dispatch={dispatch} disabled={locked} />
                  ) : (
                    <>
                      {formatCents(precioEfectivoCents(line))}
                      {esPorPeso(line.product.base_units_per_sale_unit)
                        ? `/${unitLabel(line)}`
                        : null}
                    </>
                  )}
                </td>
                {canDiscount ? (
                  <td className="px-3 py-2">
                    <MoneyInput
                      label={`Descuento de ${line.product.name}`}
                      cents={line.discountCents}
                      disabled={locked}
                      onCents={(c) => dispatch({ type: "SET_LINE_DISCOUNT", productId: id, cents: c })}
                      className="ml-auto h-9 w-28 rounded-lg border border-pos-border bg-pos-field px-2 text-right text-sm text-pos-text disabled:opacity-50"
                    />
                  </td>
                ) : null}
                <td className="px-3 py-2 text-right font-semibold text-pos-text">
                  {formatCents(lineNetCents(line))}
                </td>
                <td className="px-3 py-2 text-right">
                  <button
                    type="button"
                    aria-label={`Quitar ${line.product.name}`}
                    disabled={locked}
                    className="rounded p-1 text-pos-muted hover:text-pos-danger disabled:opacity-40"
                    onClick={() => {
                      dispatch({ type: "REMOVE", productId: id });
                      onAfterAction?.();
                    }}
                  >
                    <Trash2 className="h-4 w-4" />
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
