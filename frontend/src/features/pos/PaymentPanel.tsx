"use client";

import { useEffect, useState } from "react";

import {
  POS_PAYMENT_METHODS,
  type SaleAction,
  type SaleState,
  cashPartCents,
  formatCents,
  hasCash,
  hasFiado,
  isSplit,
  toCents,
  unassignedCents,
} from "@/lib/pos/cart";
import { PAYMENT_METHOD_LABELS } from "@/lib/suppliers";
import { type PosCustomer, posService } from "@/services/pos.service";

interface PaymentPanelProps {
  sale: SaleState;
  totalCents: number;
  dispatch: (a: SaleAction) => void;
  locked: boolean;
  canFiado: boolean;
}

function etiqueta(method: string): string {
  const label = PAYMENT_METHOD_LABELS[method] ?? method;
  return method === "account" ? "Fiado (cuenta corriente)" : label;
}

/**
 * Uno o varios medios de pago (B9). Con uno solo, la caja de siempre: el pago
 * sigue al total. «Dividir pago» agrega filas con importe propio; el vuelto se
 * calcula contra la parte EN EFECTIVO y, si el total cambia después, lo que
 * falte o sobre se muestra en vez de redistribuirse solo.
 */
export function PaymentPanel({ sale, totalCents, dispatch, locked, canFiado }: PaymentPanelProps) {
  const permitidos = POS_PAYMENT_METHODS.filter((m) => m !== "account" || canFiado);
  const dividido = isSplit(sale);
  const usados = new Set(sale.payments.map((p) => p.method));
  const hayLibre = permitidos.some((m) => !usados.has(m));
  const resto = unassignedCents(sale, totalCents);
  const efectivo = cashPartCents(sale, totalCents);
  const recibido = toCents(sale.cashReceived);
  const vuelto = sale.cashReceived.trim() !== "" && recibido !== null ? recibido - efectivo : null;

  return (
    <div className="space-y-3">
      {sale.payments.map((p, i) => {
        // Cada método una sola vez: la opción usada por OTRA fila no se ofrece.
        const opciones = permitidos.filter((m) => m === p.method || !usados.has(m));
        return (
          <div key={p.id} className="flex items-end gap-2">
            <label className="flex flex-1 flex-col gap-1 text-sm text-pos-text-2">
              {dividido ? `Pago ${i + 1}` : "Medio de pago"}
              <select
                aria-label={dividido ? `Medio del pago ${i + 1}` : "Medio de pago"}
                className="h-10 rounded-lg border border-pos-border bg-pos-field px-2 text-sm text-pos-text"
                value={p.method}
                disabled={locked}
                onChange={(e) =>
                  dispatch({ type: "SET_PAYMENT_METHOD", id: p.id, method: e.target.value })
                }
              >
                {opciones.map((m) => (
                  <option key={m} value={m}>
                    {etiqueta(m)}
                  </option>
                ))}
              </select>
            </label>
            {dividido ? (
              <>
                <input
                  aria-label={`Importe del pago ${i + 1}`}
                  data-pos-scan="capture"
                  inputMode="decimal"
                  placeholder="$"
                  className="h-10 w-28 rounded-lg border border-pos-border bg-pos-field px-2 text-right text-sm text-pos-text disabled:opacity-50"
                  value={p.amount}
                  disabled={locked}
                  onChange={(e) =>
                    dispatch({ type: "SET_PAYMENT_AMOUNT", id: p.id, value: e.target.value })
                  }
                />
                <button
                  type="button"
                  aria-label={`Quitar el pago ${i + 1}`}
                  className="h-10 px-2 text-sm text-pos-link disabled:opacity-40"
                  disabled={locked}
                  onClick={() => dispatch({ type: "REMOVE_PAYMENT", id: p.id })}
                >
                  Quitar
                </button>
              </>
            ) : null}
          </div>
        );
      })}

      <div className="flex items-center justify-between text-sm">
        <button
          type="button"
          className="text-pos-link underline disabled:opacity-40"
          disabled={locked || !hayLibre}
          onClick={() => dispatch({ type: "SPLIT_PAYMENT" })}
        >
          {dividido ? "Agregar otro medio" : "Dividir pago"}
        </button>
        {dividido && resto !== null && resto !== 0 ? (
          <span className="text-pos-danger" data-testid="pago-sin-asignar">
            {resto > 0 ? `Falta asignar ${formatCents(resto)}` : `Sobra ${formatCents(-resto)}`}
          </span>
        ) : null}
      </div>

      {hasCash(sale) ? (
        <div className="space-y-1">
          <label className="flex items-center justify-between gap-3 text-sm text-pos-text-2">
            <span>Efectivo recibido</span>
            <input
              aria-label="Efectivo recibido"
              data-pos-scan="capture"
              inputMode="decimal"
              placeholder="Opcional"
              className="h-10 w-36 rounded-lg border border-pos-border bg-pos-field px-2 text-right text-sm text-pos-text disabled:opacity-50"
              value={sale.cashReceived}
              disabled={locked}
              onChange={(e) => dispatch({ type: "SET_CASH_RECEIVED", value: e.target.value })}
            />
          </label>
          {vuelto !== null ? (
            <p
              className={`text-right text-sm ${vuelto < 0 ? "text-pos-danger" : "text-pos-text"}`}
              data-testid="vuelto-preview"
            >
              {vuelto < 0 ? `Faltan ${formatCents(-vuelto)}` : `Vuelto: ${formatCents(vuelto)}`}
            </p>
          ) : null}
        </div>
      ) : null}

      {hasFiado(sale) ? (
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
        <span className="text-pos-text">Fiado a: {selectedName}</span>
        <button
          type="button"
          className="text-pos-link underline disabled:opacity-40"
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
        className="h-10 w-full rounded-lg border border-pos-border bg-pos-field px-2 text-sm text-pos-text"
        value={q}
        disabled={disabled}
        onChange={(e) => setQ(e.target.value)}
      />
      <ul className="max-h-40 overflow-auto">
        {items.map((c) => (
          <li key={c.id}>
            <button
              type="button"
              className="w-full rounded px-2 py-1 text-left text-sm hover:bg-pos-hover"
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
