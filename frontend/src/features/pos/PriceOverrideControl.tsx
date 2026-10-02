"use client";

/**
 * Cobrar una línea a otro precio (permiso `price_override`).
 *
 * Pide el motivo junto con el precio y no deja aplicar sin él: el servidor lo
 * exige (`PRICE_OVERRIDE_REASON_REQUIRED`), y es lo único que explica después
 * por qué esa venta se cobró distinto. El precio de lista sigue a la vista.
 */

import { useState } from "react";

import { MoneyInput } from "@/features/pos/DiscountControl";
import { type CartLine, type SaleAction, formatCents, toCents } from "@/lib/pos/cart";
import { esPorPeso, etiquetaDeUnidad } from "@/lib/pos/saleUnit";

interface Props {
  line: CartLine;
  dispatch: (a: SaleAction) => void;
  disabled: boolean;
}

export function PriceOverrideControl({ line, dispatch, disabled }: Props) {
  const [abierto, setAbierto] = useState(false);
  const [cents, setCents] = useState(0);
  const [motivo, setMotivo] = useState("");
  const id = line.product.id;
  const lista = toCents(line.product.sale_price_ars) ?? 0;
  const manual = line.manualPriceCents ?? null;

  if (!abierto) {
    return (
      <div className="flex flex-col items-end">
        <span className={manual !== null ? "font-medium text-pos-text" : "text-pos-text-2"}>
          {formatCents(manual ?? lista)}
          {esPorPeso(line.product.base_units_per_sale_unit)
            ? `/${etiquetaDeUnidad(line.product.sale_unit, line.product.base_units_per_sale_unit)}`
            : null}
        </span>
        {manual !== null ? (
          <span className="text-xs text-pos-muted" title={line.manualPriceReason}>
            lista {formatCents(lista)} ·{" "}
            <button
              type="button"
              className="text-pos-link underline disabled:opacity-40"
              disabled={disabled}
              onClick={() => dispatch({ type: "SET_LINE_PRICE", productId: id, cents: null, reason: "" })}
            >
              quitar
            </button>
          </span>
        ) : (
          <button
            type="button"
            className="text-xs text-pos-link underline disabled:opacity-40"
            disabled={disabled}
            onClick={() => {
              setCents(lista);
              setMotivo("");
              setAbierto(true);
            }}
          >
            cambiar precio
          </button>
        )}
      </div>
    );
  }

  const valido = cents > 0 && motivo.trim() !== "";
  return (
    <div className="flex flex-col items-end gap-1">
      <MoneyInput
        label={`Precio de ${line.product.name}`}
        cents={cents}
        onCents={setCents}
        disabled={disabled}
        className="h-8 w-28 rounded-lg border border-pos-border bg-pos-field px-2 text-right text-sm text-pos-text"
      />
      <input
        aria-label={`Motivo del precio de ${line.product.name}`}
        className="h-8 w-40 rounded-lg border border-pos-border bg-pos-field px-2 text-xs text-pos-text"
        placeholder="Motivo (obligatorio)"
        maxLength={200}
        value={motivo}
        onChange={(e) => setMotivo(e.target.value)}
      />
      <div className="flex gap-2 text-xs">
        <button
          type="button"
          className="text-pos-link underline disabled:opacity-40"
          disabled={!valido || disabled}
          onClick={() => {
            dispatch({ type: "SET_LINE_PRICE", productId: id, cents, reason: motivo });
            setAbierto(false);
          }}
        >
          Aplicar
        </button>
        <button type="button" className="text-pos-muted underline" onClick={() => setAbierto(false)}>
          Cancelar
        </button>
      </div>
    </div>
  );
}
