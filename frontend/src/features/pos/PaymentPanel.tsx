"use client";

import { useEffect, useState } from "react";

import { type SaleAction, type SaleState, formatCents, toCents } from "@/lib/pos/cart";
import { PAYMENT_METHOD_LABELS } from "@/lib/suppliers";
import { type PosCustomer, posService } from "@/services/pos.service";

interface PaymentPanelProps {
  sale: SaleState;
  totalCents: number;
  dispatch: (a: SaleAction) => void;
  locked: boolean;
  canFiado: boolean;
}

/**
 * Un solo medio de pago: el mixto se rechaza en el servidor hasta B9, así que
 * la pantalla no lo ofrece.
 */
export function PaymentPanel({ sale, totalCents, dispatch, locked, canFiado }: PaymentPanelProps) {
  const metodos = Object.entries(PAYMENT_METHOD_LABELS).filter(
    ([value]) => value !== "account" || canFiado,
  );
  const recibido = toCents(sale.cashReceived);
  const vuelto = sale.cashReceived.trim() !== "" && recibido !== null ? recibido - totalCents : null;

  return (
    <div className="space-y-3">
      <label className="flex flex-col gap-1 text-sm text-vk-text-secondary">
        Medio de pago
        <select
          aria-label="Medio de pago"
          className="h-10 rounded-lg border border-vk-border-w bg-vk-surface-w px-2 text-sm text-vk-text-primary"
          value={sale.paymentMethod}
          disabled={locked}
          onChange={(e) => dispatch({ type: "SET_PAYMENT", method: e.target.value })}
        >
          {metodos.map(([value, label]) => (
            <option key={value} value={value}>
              {label === "Cuenta corriente" ? "Fiado (cuenta corriente)" : label}
            </option>
          ))}
        </select>
      </label>

      {sale.paymentMethod === "cash" ? (
        <div className="space-y-1">
          <label className="flex items-center justify-between gap-3 text-sm text-vk-text-secondary">
            <span>Efectivo recibido</span>
            <input
              aria-label="Efectivo recibido"
              data-pos-scan="capture"
              inputMode="decimal"
              placeholder="Opcional"
              className="h-10 w-36 rounded-lg border border-vk-border-w bg-vk-surface-w px-2 text-right text-sm text-vk-text-primary disabled:opacity-50"
              value={sale.cashReceived}
              disabled={locked}
              onChange={(e) => dispatch({ type: "SET_CASH_RECEIVED", value: e.target.value })}
            />
          </label>
          {vuelto !== null ? (
            <p
              className={`text-right text-sm ${vuelto < 0 ? "text-vk-danger" : "text-vk-text-primary"}`}
              data-testid="vuelto-preview"
            >
              {vuelto < 0 ? `Faltan ${formatCents(-vuelto)}` : `Vuelto: ${formatCents(vuelto)}`}
            </p>
          ) : null}
        </div>
      ) : null}

      {sale.paymentMethod === "account" ? (
        <CustomerPicker
          selectedName={sale.customerName}
          disabled={locked}
          onSelect={(c) => dispatch({ type: "SET_CUSTOMER", id: c?.id ?? null, name: c?.name ?? null })}
        />
      ) : null}
    </div>
  );
}

function CustomerPicker({
  selectedName,
  disabled,
  onSelect,
}: {
  selectedName: string | null;
  disabled: boolean;
  onSelect: (c: PosCustomer | null) => void;
}) {
  const [q, setQ] = useState("");
  const [items, setItems] = useState<PosCustomer[]>([]);

  useEffect(() => {
    if (selectedName || q.trim().length < 2) {
      setItems([]);
      return;
    }
    let vigente = true;
    const t = setTimeout(() => {
      posService
        .searchCustomers(q.trim())
        .then((r) => vigente && setItems(r))
        .catch(() => vigente && setItems([]));
    }, 250);
    return () => {
      vigente = false;
      clearTimeout(t);
    };
  }, [q, selectedName]);

  if (selectedName) {
    return (
      <div className="flex items-center justify-between text-sm">
        <span className="text-vk-text-primary">Fiado a: {selectedName}</span>
        <button
          type="button"
          className="text-vk-blue underline disabled:opacity-40"
          disabled={disabled}
          onClick={() => onSelect(null)}
        >
          Cambiar
        </button>
      </div>
    );
  }
  return (
    <div className="space-y-1">
      <input
        aria-label="Buscar cliente"
        placeholder="Cliente (nombre)"
        className="h-10 w-full rounded-lg border border-vk-border-w bg-vk-surface-w px-2 text-sm text-vk-text-primary"
        value={q}
        disabled={disabled}
        onChange={(e) => setQ(e.target.value)}
      />
      <ul className="max-h-40 overflow-auto">
        {items.map((c) => (
          <li key={c.id}>
            <button
              type="button"
              className="w-full rounded px-2 py-1 text-left text-sm hover:bg-vk-bg-light"
              onClick={() => {
                onSelect(c);
                setQ("");
              }}
            >
              {c.name}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
