"use client";

import { useEffect, useState } from "react";

import { centsToAmount, toCents } from "@/lib/pos/cart";
import { aUnidadesBase, enUnidadDeVenta, esPorPeso } from "@/lib/pos/saleUnit";

/**
 * Inputs numéricos de la caja. Todos llevan `data-pos-scan="capture"`: si el
 * lector dispara con el foco acá, `useScanner` restaura el valor y procesa la
 * lectura, en vez de dejar el código metido en un importe.
 */

const INPUT =
  "h-9 w-full rounded-lg border border-pos-border bg-pos-field px-2 text-right text-sm " +
  "text-pos-text focus:outline-none focus:ring-2 focus:ring-vk-blue/20 " +
  "disabled:opacity-50";

interface MoneyInputProps {
  cents: number;
  onCents: (cents: number) => void;
  disabled?: boolean;
  label: string;
  className?: string;
}

/** Monto en pesos. Guarda el texto mientras se tipea ("12," es válido a medias). */
export function MoneyInput({ cents, onCents, disabled, label, className }: MoneyInputProps) {
  const [text, setText] = useState(cents ? centsToAmount(cents) : "");

  // Si el valor cambia desde afuera (venta nueva), el texto lo sigue.
  useEffect(() => {
    const actual = text.trim() === "" ? 0 : toCents(text);
    if (actual !== cents) setText(cents ? centsToAmount(cents) : "");
    // Sólo cuando cambia el valor externo: `text` es el estado local.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cents]);

  return (
    <input
      aria-label={label}
      data-pos-scan="capture"
      inputMode="decimal"
      className={className ?? INPUT}
      disabled={disabled}
      placeholder="0"
      value={text}
      onChange={(e) => {
        setText(e.target.value);
        const v = e.target.value.trim() === "" ? 0 : toCents(e.target.value);
        if (v !== null) onCents(v);
      }}
    />
  );
}

interface QuantityInputProps {
  /** En UNIDADES BASE. */
  quantity: number;
  onQuantity: (q: number) => void;
  disabled?: boolean;
  label: string;
  /** Unidades base por unidad de venta (1000 = se tipea en kg o L). */
  factor?: number;
}

export function QuantityInput({
  quantity,
  onQuantity,
  disabled,
  label,
  factor = 1,
}: QuantityInputProps) {
  // La cantidad viaja en unidades BASE (como el servidor); el campo se escribe
  // en la unidad de venta. En un producto por peso, "0,750" kg son 750 g.
  const [text, setText] = useState(enUnidadDeVenta(quantity, factor));

  useEffect(() => {
    if (aUnidadesBase(text, factor) !== quantity) setText(enUnidadDeVenta(quantity, factor));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [quantity, factor]);

  return (
    <input
      aria-label={label}
      data-pos-scan="capture"
      inputMode={esPorPeso(factor) ? "decimal" : "numeric"}
      className={`${INPUT} w-20`}
      disabled={disabled}
      value={text}
      onChange={(e) => {
        setText(e.target.value);
        const base = aUnidadesBase(e.target.value, factor);
        if (base !== null) onQuantity(base);
      }}
      // Un campo que quedó vacío o en 0 vuelve a la cantidad vigente.
      onBlur={() => setText(enUnidadDeVenta(quantity, factor))}
    />
  );
}

interface DiscountControlProps {
  cents: number;
  onCents: (cents: number) => void;
  disabled?: boolean;
}

/** Descuento sobre el total de la venta. Sólo se monta con el permiso `discount`. */
export function DiscountControl({ cents, onCents, disabled }: DiscountControlProps) {
  return (
    <label className="flex items-center justify-between gap-3 text-sm text-pos-text-2">
      <span>Descuento a la venta</span>
      <MoneyInput
        label="Descuento a la venta"
        cents={cents}
        onCents={onCents}
        disabled={disabled}
        className={`${INPUT} w-32`}
      />
    </label>
  );
}
