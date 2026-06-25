"use client";

import { useEffect, useState } from "react";
import axios from "axios";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Modal } from "@/components/ui/Modal";
import { cashCloseService } from "@/services/cashClose.service";
import { paymentLabel } from "@/lib/payment";
import { isFiscallyRegistered } from "@/lib/fiscalCondition";
import { useToastStore } from "@/stores/toastStore";

function formatARS(value: number): string {
  return new Intl.NumberFormat("es-AR", {
    style: "currency",
    currency: "ARS",
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  }).format(value);
}

function todayISO(): string {
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

/** Billetes en circulación (ARS), de mayor a menor. */
const DENOMINATIONS = [20000, 10000, 2000, 1000, 500, 200, 100, 50, 20, 10] as const;

const inputCls =
  "w-full rounded-lg border border-vk-border-w bg-vk-night py-2 pl-7 pr-3 text-sm " +
  "text-vektor-white focus:outline-none focus:ring-2 focus:ring-vk-blue/20";

interface CashCloseModalProps {
  isOpen: boolean;
  onClose: () => void;
}

export function CashCloseModal({ isOpen, onClose }: CashCloseModalProps) {
  const qc = useQueryClient();
  const toast = useToastStore();
  const closeDate = todayISO();

  const [simpleMode, setSimpleMode] = useState(false);
  // Modo simple (legacy): un solo total contado.
  const [counted, setCounted] = useState<string>("");
  // Arqueo: paso 1 — fondo de caja inicial (fijo, no es ganancia del día).
  const [openingFloat, setOpeningFloat] = useState<string>("");
  // Paso 2 — conteo de efectivo por denominación. Key = denominación.
  const [denoms, setDenoms] = useState<Record<string, string>>({});
  // Paso 3 — digitales contados por método + vales de gasto desde caja.
  const [countedByMethod, setCountedByMethod] = useState<Record<string, string>>({});
  const [vouchers, setVouchers] = useState<string>("");
  const [registerSurplus, setRegisterSurplus] = useState(false);
  const [notes, setNotes] = useState<string>("");

  const { data: preview, isLoading } = useQuery({
    queryKey: ["cash-preview", closeDate],
    queryFn: () => cashCloseService.getPreview(closeDate),
    enabled: isOpen,
    staleTime: 30 * 1000,
  });

  // Prellenar el fondo con el del último arqueo (es fijo día a día).
  useEffect(() => {
    if (isOpen && preview?.suggested_opening_float_ars != null && openingFloat === "") {
      setOpeningFloat(String(preview.suggested_opening_float_ars));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen, preview?.suggested_opening_float_ars]);

  const digitalMethods = (preview?.breakdown ?? []).filter(
    (b) => b.payment_method !== "cash",
  );

  // ── Cálculo del arqueo (espejo de la fórmula server-side) ──────────────────
  const physicalCash = DENOMINATIONS.reduce((sum, d) => {
    const qty = parseInt(denoms[String(d)] ?? "", 10);
    return sum + (Number.isFinite(qty) ? d * qty : 0);
  }, 0);
  const openingNum = parseFloat(openingFloat) || 0;
  const vouchersNum = parseFloat(vouchers) || 0;
  const cashNet = physicalCash - openingNum + vouchersNum;
  const digitalSum = digitalMethods.reduce((sum, b) => {
    const v = parseFloat(countedByMethod[b.payment_method] ?? "");
    return sum + (Number.isFinite(v) ? v : 0);
  }, 0);

  const expected = preview?.expected_total_ars ?? 0;
  const countedNum = simpleMode ? parseFloat(counted) : cashNet + digitalSum;
  const hasCounted = simpleMode
    ? !isNaN(parseFloat(counted))
    : Object.values(denoms).some((v) => v !== "") ||
      Object.values(countedByMethod).some((v) => v !== "");
  const difference = hasCounted ? countedNum - expected : 0;

  const resultLabel =
    difference === 0
      ? { text: "Caja cuadrada", cls: "text-vk-text-secondary" }
      : difference > 0
        ? { text: "Sobrante de caja", cls: "text-vk-success" }
        : { text: "Faltante de caja", cls: "text-vk-danger" };

  const createMutation = useMutation({
    mutationFn: () => {
      if (simpleMode) {
        return cashCloseService.createClose({
          close_date: closeDate,
          counted_total_ars: parseFloat(counted) || 0,
          notes: notes.trim() || null,
        });
      }
      const byMethod: Record<string, number> = {};
      for (const [method, raw] of Object.entries(countedByMethod)) {
        const num = parseFloat(raw);
        if (!isNaN(num)) byMethod[method] = num;
      }
      const cashDenoms: Record<string, number> = {};
      for (const d of DENOMINATIONS) {
        const qty = parseInt(denoms[String(d)] ?? "", 10);
        if (Number.isFinite(qty) && qty > 0) cashDenoms[String(d)] = qty;
      }
      return cashCloseService.createClose({
        close_date: closeDate,
        // El backend recomputa el contado server-side desde el arqueo.
        counted_total_ars: Math.max(0, countedNum),
        counted_by_method: Object.keys(byMethod).length > 0 ? byMethod : undefined,
        opening_float_ars: parseFloat(openingFloat) || 0,
        cash_denominations: cashDenoms,
        voucher_expenses_ars: vouchersNum,
        register_surplus_as_income: registerSurplus && difference > 0,
        notes: notes.trim() || null,
      });
    },
    onSuccess: async (result) => {
      toast.add(
        result.surplus_registered
          ? "Cierre registrado. El sobrante quedó como otros ingresos."
          : "Cierre de caja registrado.",
        "success",
      );
      await qc.invalidateQueries({ queryKey: ["cash-preview"] });
      await qc.invalidateQueries({ queryKey: ["cash-closes-today"] });
      if (result.surplus_registered) {
        await qc.invalidateQueries({ queryKey: ["sales-entries"] });
      }
      setCounted("");
      setNotes("");
      setDenoms({});
      setCountedByMethod({});
      setVouchers("");
      setRegisterSurplus(false);
      onClose();
    },
    onError: (err: unknown) => {
      if (axios.isAxiosError(err) && err.response?.status === 409) {
        toast.add("Ya registraste el cierre de caja de hoy.", "error");
      } else {
        toast.add("No se pudo registrar el cierre.", "error");
      }
    },
  });

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="Cierre de caja" size="lg">
      {isLoading ? (
        <p className="py-8 text-center text-sm text-vk-text-muted">Calculando…</p>
      ) : preview?.already_closed ? (
        <p className="py-8 text-center text-sm text-vektor-body">
          Ya registraste el cierre de caja de hoy.
        </p>
      ) : (
        <div className="space-y-5">
          {/* Esperado por método */}
          <div>
            <p className="mb-2 text-xs uppercase tracking-wide text-vk-text-muted">
              Esperado según el sistema
            </p>
            {preview && preview.breakdown.length > 0 ? (
              <div className="space-y-1.5">
                {preview.breakdown.map((b) => (
                  <div key={b.payment_method} className="flex justify-between text-sm">
                    <span className="text-vektor-body">
                      {paymentLabel(b.payment_method)}
                    </span>
                    <span className="tabular-nums text-vektor-white">
                      {formatARS(b.expected_ars)}
                    </span>
                  </div>
                ))}
                <div className="mt-2 flex justify-between border-t border-vk-border-w pt-2 text-sm font-semibold">
                  <span className="text-vektor-white">Total esperado</span>
                  <span className="tabular-nums text-vektor-white">
                    {formatARS(expected)}
                  </span>
                </div>
              </div>
            ) : (
              <p className="text-sm text-vk-text-muted">
                No hay ventas registradas hoy. El esperado es {formatARS(0)}.
              </p>
            )}
          </div>

          <div className="flex justify-end">
            <button
              type="button"
              onClick={() => setSimpleMode((s) => !s)}
              className="text-xs text-vk-blue hover:underline"
            >
              {simpleMode ? "Hacer arqueo completo" : "Ingresar solo el total"}
            </button>
          </div>

          {simpleMode ? (
            <div>
              <label
                htmlFor="counted"
                className="mb-1.5 block text-sm font-medium text-vektor-white"
              >
                ¿Cuánto contaste en caja?
              </label>
              <div className="relative">
                <span className="absolute left-3 top-1/2 -translate-y-1/2 text-sm text-vk-text-muted">
                  $
                </span>
                <input
                  id="counted"
                  type="number"
                  inputMode="decimal"
                  value={counted}
                  onChange={(e) => setCounted(e.target.value)}
                  placeholder="0"
                  className={inputCls}
                />
              </div>
            </div>
          ) : (
            <>
              {/* Paso 1 — fondo de caja inicial */}
              <div>
                <label
                  htmlFor="opening-float"
                  className="mb-1.5 block text-sm font-medium text-vektor-white"
                >
                  1. Fondo de caja inicial
                </label>
                <p className="mb-1.5 text-xs text-vk-text-muted">
                  El cambio con el que abriste el turno. Es fijo y no cuenta como
                  ganancia del día.
                </p>
                <div className="relative">
                  <span className="absolute left-3 top-1/2 -translate-y-1/2 text-sm text-vk-text-muted">
                    $
                  </span>
                  <input
                    id="opening-float"
                    type="number"
                    inputMode="decimal"
                    value={openingFloat}
                    onChange={(e) => setOpeningFloat(e.target.value)}
                    placeholder="0"
                    className={inputCls}
                  />
                </div>
              </div>

              {/* Paso 2 — conteo de efectivo por denominación */}
              <div>
                <p className="mb-1.5 text-sm font-medium text-vektor-white">
                  2. Recuento del efectivo físico
                </p>
                <p className="mb-2 text-xs text-vk-text-muted">
                  Cantidad de billetes por denominación al cierre del turno.
                </p>
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
                  {DENOMINATIONS.map((d) => (
                    <label key={d} className="grid gap-1 text-xs text-vektor-body">
                      ${d.toLocaleString("es-AR")}
                      <input
                        type="number"
                        inputMode="numeric"
                        min={0}
                        value={denoms[String(d)] ?? ""}
                        onChange={(e) =>
                          setDenoms((prev) => ({ ...prev, [String(d)]: e.target.value }))
                        }
                        placeholder="0"
                        className="w-full rounded-lg border border-vk-border-w bg-vk-night px-2 py-1.5 text-sm text-vektor-white focus:outline-none focus:ring-2 focus:ring-vk-blue/20"
                      />
                    </label>
                  ))}
                </div>
                <p className="mt-1.5 text-right text-xs text-vk-text-muted">
                  Efectivo físico: {formatARS(physicalCash)}
                </p>
              </div>

              {/* Paso 3 — digitales y comprobantes */}
              <div>
                <p className="mb-1.5 text-sm font-medium text-vektor-white">
                  3. Pagos digitales y comprobantes
                </p>
                <p className="mb-2 text-xs text-vk-text-muted">
                  Cierre de terminales: cupones de tarjeta, transferencias y QR.
                </p>
                <div className="space-y-2">
                  {digitalMethods.map((b) => (
                    <div key={b.payment_method} className="flex items-center gap-2">
                      <span className="w-32 text-sm text-vektor-body">
                        {paymentLabel(b.payment_method)}
                      </span>
                      <div className="relative flex-1">
                        <span className="absolute left-3 top-1/2 -translate-y-1/2 text-sm text-vk-text-muted">
                          $
                        </span>
                        <input
                          type="number"
                          inputMode="decimal"
                          value={countedByMethod[b.payment_method] ?? ""}
                          onChange={(e) =>
                            setCountedByMethod((prev) => ({
                              ...prev,
                              [b.payment_method]: e.target.value,
                            }))
                          }
                          placeholder={String(b.expected_ars)}
                          className={inputCls}
                        />
                      </div>
                    </div>
                  ))}
                  <div className="flex items-center gap-2">
                    <span className="w-32 text-sm text-vektor-body">Vales de gasto</span>
                    <div className="relative flex-1">
                      <span className="absolute left-3 top-1/2 -translate-y-1/2 text-sm text-vk-text-muted">
                        $
                      </span>
                      <input
                        type="number"
                        inputMode="decimal"
                        value={vouchers}
                        onChange={(e) => setVouchers(e.target.value)}
                        placeholder="0"
                        className={inputCls}
                      />
                    </div>
                  </div>
                  <p className="text-xs text-vk-text-muted">
                    Vales: salidas de caja con comprobante que NO registraste como gasto
                    en Véktor (las registradas ya se descuentan solas).
                  </p>
                </div>
              </div>

              {/* Paso 4 — balance */}
              {hasCounted && (
                <div className="space-y-2 rounded-lg border border-vk-border-w bg-vk-surface-w px-4 py-3">
                  <div className="flex justify-between text-xs text-vk-text-muted">
                    <span>
                      Efectivo {formatARS(physicalCash)} − fondo {formatARS(openingNum)} +
                      vales {formatARS(vouchersNum)} + digitales {formatARS(digitalSum)}
                    </span>
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-sm text-vk-text-secondary">
                      4. Balance — <span className={resultLabel.cls}>{resultLabel.text}</span>
                    </span>
                    <span
                      className={`text-lg font-semibold tabular-nums ${resultLabel.cls}`}
                    >
                      {difference > 0 ? "+" : ""}
                      {formatARS(difference)}
                    </span>
                  </div>
                  {difference > 0 && (
                    <label className="flex items-center gap-2 text-sm text-vk-text-secondary">
                      <input
                        type="checkbox"
                        checked={registerSurplus}
                        onChange={(e) => setRegisterSurplus(e.target.checked)}
                      />
                      Registrar el sobrante como otros ingresos
                    </label>
                  )}
                  {difference < 0 && (
                    <p className="text-xs text-vk-text-muted">
                      El faltante queda anotado en el cierre como pérdida interna
                      (vuelto mal dado, gasto/retiro sin anotar, o hurto).
                    </p>
                  )}
                </div>
              )}

              {/* Guía según condición fiscal */}
              <details className="rounded-lg border border-vk-border-w bg-vk-surface-w px-4 py-3 text-sm">
                <summary className="cursor-pointer text-vk-text-secondary">
                  Guía del arqueo{" "}
                  {isFiscallyRegistered(preview?.fiscal_condition)
                    ? "(negocio registrado)"
                    : preview?.fiscal_condition === "informal"
                      ? "(control interno)"
                      : ""}
                </summary>
                <div className="mt-2 space-y-1.5 text-xs text-vk-text-muted">
                  {isFiscallyRegistered(preview?.fiscal_condition) ? (
                    <>
                      <p>• Sumá solo comprobantes válidos: facturas A/B/C, tiques de controlador fiscal o factura electrónica. Los &quot;X&quot; y remitos no cuentan como venta legal.</p>
                      <p>• El total de cobros por QR o tarjeta debe coincidir con el cierre de lote de tu terminal y los reportes de las billeteras.</p>
                      <p>• Todo gasto pagado con efectivo de la caja necesita tique factura o factura para que tu contador pueda computarlo.</p>
                    </>
                  ) : preview?.fiscal_condition === "informal" ? (
                    <>
                      <p>• El arqueo es tu control interno: tiques &quot;X&quot;, cuadernos y comandas son tu única métrica — anotá todo.</p>
                      <p>• Los retiros para compras personales o pagos a proveedores van en vales: si el vale no está, la caja falta y no vas a saber por qué.</p>
                    </>
                  ) : (
                    <p>
                      Configurá la condición fiscal de tu negocio en Configuración para
                      ver la guía de comprobantes que te corresponde.
                    </p>
                  )}
                  <p className="pt-1 font-semibold text-vk-text-secondary">
                    Véktor no valida comprobantes ni liquida impuestos — para eso,
                    consultá a tu contador.
                  </p>
                </div>
              </details>
            </>
          )}

          {/* Notas */}
          <div>
            <label
              htmlFor="cash-notes"
              className="mb-1.5 block text-sm font-medium text-vektor-white"
            >
              Notas (opcional)
            </label>
            <textarea
              id="cash-notes"
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              rows={2}
              maxLength={500}
              placeholder="Ej: faltó vuelto, propina incluida…"
              className="w-full rounded-lg border border-vk-border-w bg-vk-night px-3 py-2 text-sm text-vektor-white focus:outline-none focus:ring-2 focus:ring-vk-blue/20"
            />
          </div>

          <div className="flex justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded-full border border-vk-border-w px-5 py-2 text-sm font-medium text-vektor-body hover:text-vektor-white"
            >
              Cancelar
            </button>
            <button
              type="button"
              disabled={!hasCounted || createMutation.isPending}
              onClick={() => createMutation.mutate()}
              className="rounded-full bg-vk-blue px-5 py-2 text-sm font-medium text-white disabled:opacity-50"
            >
              {createMutation.isPending ? "Guardando…" : "Cerrar caja"}
            </button>
          </div>
        </div>
      )}
    </Modal>
  );
}
