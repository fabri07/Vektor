"use client";

import { Trash2 } from "lucide-react";

import { MoneyInput, QuantityInput } from "@/features/pos/DiscountControl";
import {
  type CartLine,
  type SaleAction,
  formatCents,
  lineNetCents,
  toCents,
} from "@/lib/pos/cart";

interface CartTableProps {
  lines: CartLine[];
  dispatch: (a: SaleAction) => void;
  /** El carrito sólo se toca en `editando`: en duda o enviando queda bloqueado. */
  locked: boolean;
  canDiscount: boolean;
  onAfterAction?: () => void;
}

function unitLabel(line: CartLine): string {
  if (line.product.base_units_per_sale_unit <= 1) return "u.";
  return line.product.sale_unit === "milliliter" ? "ml" : line.product.sale_unit === "gram" ? "g" : "u.";
}

export function CartTable({ lines, dispatch, locked, canDiscount, onAfterAction }: CartTableProps) {
  if (lines.length === 0) {
    return (
      <div className="flex flex-1 items-center justify-center rounded-xl border border-dashed border-vk-border-w p-10 text-sm text-vk-text-muted">
        Escaneá un producto o buscalo por nombre.
      </div>
    );
  }

  return (
    <div className="overflow-auto rounded-xl border border-vk-border-w bg-vk-surface-w">
      <table className="w-full text-sm">
        <thead className="text-left text-xs uppercase tracking-wide text-vk-text-muted">
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
              <tr key={id} className="border-t border-vk-border-w" data-testid="cart-line">
                <td className="px-3 py-2">
                  <p className="font-medium text-vk-text-primary">{line.product.name}</p>
                  {sinStock ? (
                    <p className="text-xs text-vk-danger">
                      Stock disponible: {line.product.stock_display ?? line.product.stock_units}
                    </p>
                  ) : null}
                </td>
                <td className="px-3 py-2">
                  <div className="flex items-center gap-1">
                    <QuantityInput
                      label={`Cantidad de ${line.product.name}`}
                      quantity={line.quantity}
                      disabled={locked}
                      onQuantity={(q) => dispatch({ type: "SET_QUANTITY", productId: id, quantity: q })}
                    />
                    <span className="text-xs text-vk-text-muted">{unitLabel(line)}</span>
                  </div>
                </td>
                <td className="px-3 py-2 text-right text-vk-text-secondary">
                  {formatCents(toCents(line.product.sale_price_ars) ?? 0)}
                </td>
                {canDiscount ? (
                  <td className="px-3 py-2">
                    <MoneyInput
                      label={`Descuento de ${line.product.name}`}
                      cents={line.discountCents}
                      disabled={locked}
                      onCents={(c) => dispatch({ type: "SET_LINE_DISCOUNT", productId: id, cents: c })}
                      className="ml-auto h-9 w-28 rounded-lg border border-vk-border-w bg-vk-surface-w px-2 text-right text-sm text-vk-text-primary disabled:opacity-50"
                    />
                  </td>
                ) : null}
                <td className="px-3 py-2 text-right font-semibold text-vk-text-primary">
                  {formatCents(lineNetCents(line))}
                </td>
                <td className="px-3 py-2 text-right">
                  <button
                    type="button"
                    aria-label={`Quitar ${line.product.name}`}
                    disabled={locked}
                    className="rounded p-1 text-vk-text-muted hover:text-vk-danger disabled:opacity-40"
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
