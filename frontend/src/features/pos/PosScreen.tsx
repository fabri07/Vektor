"use client";

import Link from "next/link";
import { useCallback, useEffect, useReducer, useRef, useState } from "react";

import { Button } from "@/components/ui/Button";
import { CartTable } from "@/features/pos/CartTable";
import { CashierHome } from "@/features/pos/CashierHome";
import { DiscountControl } from "@/features/pos/DiscountControl";
import { PaymentPanel } from "@/features/pos/PaymentPanel";
import { type PickerMode, ProductPicker } from "@/features/pos/ProductPicker";
import { RecentTickets } from "@/features/pos/RecentTickets";
import { ScanInput } from "@/features/pos/ScanInput";
import { VoidLastTicket } from "@/features/pos/VoidLastTicket";
import { usePosPrinting } from "@/features/pos/usePosPrinting";
import { useScanner } from "@/features/pos/useScanner";
import {
  type CartLine,
  type PosOperationPayload,
  type PosOperationResult,
  type PosProduct,
  buildPayload,
  cannotCharge,
  formatCents,
  initialSale,
  isDefinitiveRejection,
  newOperationId,
  previewTotals,
  saleReducer,
  toCents,
} from "@/lib/pos/cart";
import { type PosErrorInfo, cobroRejectionMessage, posErrorInfo } from "@/lib/pos/errors";
import { clearPending, loadPending, savePending } from "@/lib/pos/pendingOperation";
import { CASHIER_ROLE, POS_ROLES, hasPosPermission } from "@/lib/roles";
import { logoutRequest } from "@/services/auth.service";
import { type ScanCandidate, posService } from "@/services/pos.service";
import { useAuthStore } from "@/stores/authStore";
import { type PageSize, type PaperWidth, usePosPrintConfigStore } from "@/stores/posPrintConfigStore";
import { usePosScannerConfigStore } from "@/stores/posScannerConfigStore";
import { usePosTerminalStore } from "@/stores/posTerminalStore";

const EN_DUDA =
  "No hubo respuesta del servidor: la venta puede haber entrado. Reintentá — si ya " +
  "estaba cobrada, no se cobra dos veces.";

/**
 * La caja (B13). Decide quién puede usarla y con qué PC; la pantalla en sí es
 * `PosRegister`.
 */
export function PosScreen() {
  const user = useAuthStore((s) => s.user);
  const terminal = usePosTerminalStore((s) => s.terminal);
  const invalid = usePosTerminalStore((s) => s.invalid);

  if (!user || !POS_ROLES.has(user.role)) {
    return (
      <div className="flex h-screen items-center justify-center p-6 text-sm text-vk-text-secondary">
        Tu usuario no puede cobrar en la caja.
      </div>
    );
  }
  // Un cajero sólo escribe desde una PC habilitada (B6). Se chequea al entrar:
  // enterarse recién al cobrar es perder el carrito armado.
  if (user.role === CASHIER_ROLE) {
    if (!terminal || terminal.tenantId !== user.tenant_id) return <CashierHome reason="no_terminal" />;
    if (invalid) return <CashierHome reason="terminal_invalid" />;
  }
  return <PosRegister />;
}

function PosRegister() {
  const user = useAuthStore((s) => s.user);
  const logout = useAuthStore((s) => s.logout);
  const terminal = usePosTerminalStore((s) => s.terminal);
  const terminalInvalid = usePosTerminalStore((s) => s.invalid);
  const { terminator, thresholdMs, setTerminator, setThresholdMs } = usePosScannerConfigStore();
  const impresora = usePosPrintConfigStore();
  const { printReceipt, printTest, printError } = usePosPrinting();

  const [sale, dispatch] = useReducer(saleReducer, initialSale);
  const saleRef = useRef(sale);
  saleRef.current = sale;
  const [picker, setPicker] = useState<PickerMode | null>(null);
  const [scanMsg, setScanMsg] = useState<string | null>(null);
  const [lastOperation, setLastOperation] = useState<PosOperationResult | null>(null);
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const scanRef = useRef<HTMLInputElement>(null);

  const tenantId = user?.tenant_id ?? "";
  const canDiscount = hasPosPermission(user, "discount");
  const canFiado = hasPosPermission(user, "fiado");
  const canLearn = hasPosPermission(user, "learn_barcode");
  const canVoid = hasPosPermission(user, "void_ticket");

  const focusScan = useCallback(() => {
    // Después de la acción: el foco vuelve a donde lee el lector.
    setTimeout(() => scanRef.current?.focus(), 0);
  }, []);

  // ─── Cobro ────────────────────────────────────────────────────────────────

  const handleRejection = useCallback((info: PosErrorInfo, lines: CartLine[]) => {
    dispatch({ type: "REJECTED", notice: cobroRejectionMessage(info) });
    if (info.code === "TENDERS_MISMATCH" && Array.isArray(info.fields.lines)) {
      // Cambió un precio entre que se agregó el producto y el cobro.
      const prices: Record<string, string> = {};
      const cambios: string[] = [];
      for (const l of info.fields.lines as { product_id: string; unit_price_list: string }[]) {
        prices[l.product_id] = l.unit_price_list;
        const antes = lines.find((x) => x.product.id === l.product_id);
        if (antes && toCents(antes.product.sale_price_ars) !== toCents(l.unit_price_list)) {
          cambios.push(
            `${antes.product.name}: ${formatCents(toCents(antes.product.sale_price_ars) ?? 0)} → ` +
              formatCents(toCents(l.unit_price_list) ?? 0),
          );
        }
      }
      dispatch({
        type: "APPLY_PRICES",
        prices,
        notice:
          `Cambió el precio de ${cambios.length ? cambios.join("; ") : "un producto"}. ` +
          "Revisá el total con el cliente y volvé a cobrar.",
      });
    }
    if (info.code === "INSUFFICIENT_STOCK" && typeof info.fields.product_id === "string") {
      dispatch({
        type: "APPLY_STOCK",
        productId: info.fields.product_id,
        available: Number(info.fields.available ?? 0),
        notice: cobroRejectionMessage(info),
      });
    }
  }, []);

  // Candado síncrono: dos clics en el mismo tick verían los dos `editando` en
  // `saleRef` (se actualiza al re-renderizar) y mandarían dos cobros.
  const enVuelo = useRef(false);
  const send = useCallback(
    async (payload: PosOperationPayload, lines: CartLine[]) => {
      if (enVuelo.current) return;
      enVuelo.current = true;
      // Se guarda ANTES de mandar: si la página se cierra con el cobro en vuelo,
      // al volver se reintenta en vez de cobrar de nuevo.
      savePending({ tenantId, payload, lines, savedAt: new Date().toISOString() });
      dispatch({ type: "SEND", payload });
      try {
        const result = await posService.createOperation(payload);
        clearPending(payload.client_operation_id);
        dispatch({ type: "SUCCESS", result });
        setLastOperation(result);
        // Recién ahora, con la venta confirmada: una impresión que falla no
        // pone en duda el cobro.
        if (usePosPrintConfigStore.getState().autoPrint) void printReceipt(result.id);
      } catch (e) {
        const info = posErrorInfo(e);
        if (isDefinitiveRejection(info, info.code)) {
          // Un 4xx: el servidor revirtió todo, no entró nada.
          clearPending(payload.client_operation_id);
          handleRejection(info, lines);
        } else {
          dispatch({
            type: "UNKNOWN",
            notice: info.code === "IDEMPOTENCY_KEY_REUSED" ? cobroRejectionMessage(info) : EN_DUDA,
          });
        }
      } finally {
        enVuelo.current = false;
        focusScan();
      }
    },
    [tenantId, handleRejection, focusScan, printReceipt],
  );

  const cobrar = useCallback(() => {
    const s = saleRef.current;
    if (s.status !== "editando") return;
    const motivo = cannotCharge(s);
    if (motivo) {
      dispatch({ type: "NOTICE", notice: motivo });
      return;
    }
    // Id y fecha nacen acá, una sola vez: el reintento reenvía este objeto.
    void send(buildPayload(s, newOperationId(), new Date()), s.lines);
  }, [send]);

  const reintentar = useCallback(() => {
    const s = saleRef.current;
    if (s.status === "en_duda" && s.payload) void send(s.payload, s.lines);
  }, [send]);

  // Al abrir: un cobro que quedó sin confirmar se resuelve ANTES que nada.
  const restaurado = useRef(false);
  useEffect(() => {
    if (restaurado.current || !tenantId) return;
    restaurado.current = true;
    const pendiente = loadPending(tenantId);
    if (pendiente) {
      dispatch({ type: "RESTORE_PENDING", payload: pendiente.payload, lines: pendiente.lines });
      void send(pendiente.payload, pendiente.lines);
    }
  }, [tenantId, send]);

  // ─── Scan ─────────────────────────────────────────────────────────────────

  const handleScan = useCallback(
    async (raw: string) => {
      const code = raw.trim();
      if (!code) return;
      const s = saleRef.current;
      if (s.status === "enviando" || s.status === "en_duda") {
        setScanMsg("Terminá el cobro en curso antes de escanear el próximo producto.");
        return;
      }
      setScanMsg(null);
      try {
        const r = await posService.lookup(code);
        dispatch({ type: "ADD_PRODUCT", product: r.product });
      } catch (e) {
        const info = posErrorInfo(e);
        if (info.code === "SCAN_AMBIGUOUS" && Array.isArray(info.fields.candidates)) {
          const candidates = (info.fields.candidates as ScanCandidate[]).map((c) => c.product);
          setPicker({ mode: "ambiguous", code, candidates });
        } else if (info.code === "SCAN_NOT_FOUND") {
          if (info.fields.learnable === true && canLearn) setPicker({ mode: "learn", code });
          // El código CRUDO a la vista: si el lector tiene otra distribución de
          // teclado (`VKT'` en vez de `VKT-`), se ve acá.
          else setScanMsg(`Código desconocido: «${code}». Avisale al encargado.`);
        } else if (info.code === "SCAN_SCALE_NOT_SUPPORTED") {
          setScanMsg(`«${code}» es un código de balanza: todavía no se pueden leer. Buscá el producto por nombre.`);
        } else if (info.status === null) {
          setScanMsg(`Sin conexión: no se pudo buscar «${code}».`);
        } else {
          setScanMsg(info.message ?? `No se pudo buscar «${code}».`);
        }
      }
    },
    [canLearn],
  );

  useScanner({
    onScan: (c) => void handleScan(c),
    enabled: picker === null,
    terminator,
    thresholdMs,
  });

  const onPick = useCallback(
    async (product: PosProduct) => {
      const actual = picker;
      setPicker(null);
      focusScan();
      if (actual?.mode !== "learn") {
        dispatch({ type: "ADD_PRODUCT", product });
        return;
      }
      try {
        const actualizado = await posService.learnBarcode(product.id, actual.code);
        dispatch({ type: "ADD_PRODUCT", product: actualizado });
        setScanMsg(`«${actual.code}» quedó vinculado a ${product.name}.`);
      } catch (e) {
        const info = posErrorInfo(e);
        if (info.code === "BARCODE_ALREADY_SET") {
          // Lo eligió a propósito: se vende igual, pero el código no se vinculó.
          dispatch({ type: "ADD_PRODUCT", product });
          setScanMsg(`${product.name} ya tiene otro código cargado: no se vinculó «${actual.code}».`);
        } else if (info.code === "BARCODE_TAKEN") {
          setScanMsg(`«${actual.code}» ya es de otro producto. Avisale al encargado.`);
        } else {
          setScanMsg(info.message ?? "No se pudo vincular el código.");
        }
      }
    },
    [picker, focusScan],
  );

  // ─── Render ───────────────────────────────────────────────────────────────

  const { totalCents } = previewTotals(sale);
  const editable = sale.status === "editando";

  async function salir(): Promise<void> {
    try {
      await logoutRequest();
    } catch {
      // El token vence solo.
    }
    logout();
  }

  return (
    <div className="flex h-screen flex-col bg-vk-bg-light">
      <header className="flex items-center justify-between border-b border-vk-border-w bg-vk-surface-w px-4 py-2">
        <div className="text-sm">
          <span className="font-semibold text-vk-text-primary">Caja</span>
          <span className="ml-2 text-vk-text-muted">
            {terminal && terminal.tenantId === tenantId && !terminalInvalid
              ? terminal.name
              : "navegador (sin caja habilitada)"}
            {" · "}
            {user?.full_name}
          </span>
        </div>
        <div className="flex items-center gap-3 text-sm">
          <details className="relative">
            <summary className="cursor-pointer text-vk-text-secondary">Impresora</summary>
            <div className="absolute right-0 z-10 mt-2 w-72 space-y-2 rounded-lg border border-vk-border-w bg-vk-surface-w p-3 shadow-vk-lg">
              <label className="flex items-center justify-between gap-2">
                Papel
                <select
                  aria-label="Ancho del papel"
                  value={impresora.paperWidth}
                  onChange={(e) => impresora.setPaperWidth(Number(e.target.value) as PaperWidth)}
                  className="rounded border border-vk-border-w bg-vk-surface-w px-1"
                >
                  <option value={80}>80 mm</option>
                  <option value={58}>58 mm</option>
                </select>
              </label>
              <label className="flex items-center justify-between gap-2">
                Página
                <select
                  aria-label="Tamaño de página"
                  value={impresora.pageSize}
                  onChange={(e) => impresora.setPageSize(e.target.value as PageSize)}
                  className="rounded border border-vk-border-w bg-vk-surface-w px-1"
                >
                  <option value="A">A · alto fijo</option>
                  <option value="B">B · angosto</option>
                  <option value="C">C · la decide el driver</option>
                </select>
              </label>
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  aria-label="Imprimir al cobrar"
                  checked={impresora.autoPrint}
                  onChange={(e) => impresora.setAutoPrint(e.target.checked)}
                />
                Imprimir al cobrar
              </label>
              <Button variant="secondary" size="sm" onClick={() => void printTest()}>
                Imprimir prueba
              </Button>
            </div>
          </details>
          <details className="relative">
            <summary className="cursor-pointer text-vk-text-secondary">Lector</summary>
            <div className="absolute right-0 z-10 mt-2 w-64 space-y-2 rounded-lg border border-vk-border-w bg-vk-surface-w p-3 shadow-vk-lg">
              <label className="flex items-center justify-between gap-2">
                Termina con
                <select
                  aria-label="Terminador del lector"
                  value={terminator}
                  onChange={(e) => setTerminator(e.target.value as "Enter" | "Tab")}
                  className="rounded border border-vk-border-w bg-vk-surface-w px-1"
                >
                  <option value="Enter">Enter</option>
                  <option value="Tab">Tab</option>
                </select>
              </label>
              <label className="flex items-center justify-between gap-2">
                Ms entre teclas
                <input
                  aria-label="Milisegundos entre teclas"
                  type="number"
                  min={10}
                  max={200}
                  value={thresholdMs}
                  onChange={(e) => setThresholdMs(Number(e.target.value))}
                  className="w-20 rounded border border-vk-border-w bg-vk-surface-w px-1 text-right"
                />
              </label>
            </div>
          </details>
          {user?.role !== CASHIER_ROLE ? (
            <Link href="/dashboard" className="text-vk-text-secondary hover:text-vk-text-primary">
              Volver a Véktor
            </Link>
          ) : null}
          <button type="button" className="text-vk-text-secondary" onClick={() => void salir()}>
            Cerrar sesión
          </button>
        </div>
      </header>

      <div className="flex flex-1 flex-col gap-4 overflow-hidden p-4 lg:flex-row">
        <section className="flex flex-1 flex-col gap-3 overflow-hidden">
          <div className="flex gap-2">
            <div className="flex-1">
              <ScanInput ref={scanRef} onCode={(c) => void handleScan(c)} />
            </div>
            <Button variant="secondary" onClick={() => setPicker({ mode: "search" })}>
              Buscar producto
            </Button>
          </div>
          {scanMsg ? (
            <p className="text-sm text-vk-warning" role="status">
              {scanMsg}
            </p>
          ) : null}
          <CartTable
            lines={sale.status === "cobrada" ? [] : sale.lines}
            dispatch={dispatch}
            locked={!editable}
            canDiscount={canDiscount}
            onAfterAction={focusScan}
          />
        </section>

        <aside className="flex w-full flex-col gap-4 rounded-xl border border-vk-border-w bg-vk-surface-w p-4 lg:w-96">
          {sale.status === "cobrada" && sale.result ? (
            <div className="space-y-3" data-testid="venta-cobrada">
              <p className="text-sm text-vk-success">Venta cobrada</p>
              <p className="text-3xl font-semibold text-vk-text-primary">
                {formatCents(toCents(sale.result.total_ars) ?? 0)}
              </p>
              {sale.result.cash_change_ars !== null ? (
                <p className="text-lg text-vk-text-primary">
                  Vuelto: {formatCents(toCents(sale.result.cash_change_ars) ?? 0)}
                </p>
              ) : null}
              <Button
                className="w-full"
                size="lg"
                onClick={() => {
                  dispatch({ type: "NEW_SALE" });
                  focusScan();
                }}
              >
                Nueva venta
              </Button>
              <Button
                variant="secondary"
                className="w-full"
                onClick={() => {
                  if (sale.result) void printReceipt(sale.result.id);
                  focusScan();
                }}
              >
                Reimprimir ticket
              </Button>
            </div>
          ) : (
            <>
              <div className="flex items-baseline justify-between">
                <span className="text-sm text-vk-text-secondary">Total</span>
                <span className="text-3xl font-semibold text-vk-text-primary" data-testid="total">
                  {formatCents(totalCents)}
                </span>
              </div>
              {canDiscount ? (
                <DiscountControl
                  cents={sale.globalDiscountCents}
                  disabled={!editable}
                  onCents={(c) => dispatch({ type: "SET_GLOBAL_DISCOUNT", cents: c })}
                />
              ) : null}
              <PaymentPanel
                sale={sale}
                totalCents={totalCents}
                dispatch={dispatch}
                locked={!editable}
                canFiado={canFiado}
              />

              {sale.notice ? (
                <p
                  role="alert"
                  className={`text-sm ${sale.status === "en_duda" ? "text-vk-warning" : "text-vk-danger"}`}
                >
                  {sale.notice}
                </p>
              ) : null}

              {sale.status === "en_duda" ? (
                <div className="space-y-2">
                  <Button className="w-full" size="lg" onClick={reintentar}>
                    Reintentar cobro
                  </Button>
                  {confirmDiscard ? (
                    <div className="space-y-2 rounded-lg border border-vk-danger/40 p-2 text-sm">
                      <p className="text-vk-text-primary">
                        Este cobro puede haber entrado. Descartalo sólo si confirmaste en Ventas que
                        no está.
                      </p>
                      <div className="flex gap-2">
                        <Button
                          variant="danger"
                          size="sm"
                          onClick={() => {
                            if (sale.payload) clearPending(sale.payload.client_operation_id);
                            dispatch({ type: "DISCARD" });
                            setConfirmDiscard(false);
                            focusScan();
                          }}
                        >
                          Descartar igual
                        </Button>
                        <Button variant="ghost" size="sm" onClick={() => setConfirmDiscard(false)}>
                          Volver
                        </Button>
                      </div>
                    </div>
                  ) : (
                    <Button variant="ghost" size="sm" onClick={() => setConfirmDiscard(true)}>
                      Descartar este cobro
                    </Button>
                  )}
                </div>
              ) : (
                <Button
                  className="w-full"
                  size="lg"
                  loading={sale.status === "enviando"}
                  disabled={sale.status !== "editando" || sale.lines.length === 0}
                  onClick={cobrar}
                >
                  Cobrar {formatCents(totalCents)}
                </Button>
              )}
            </>
          )}

          {printError ? (
            <p role="alert" className="text-sm text-vk-warning">
              {printError}
            </p>
          ) : null}

          {canVoid && lastOperation && sale.status !== "enviando" ? (
            <VoidLastTicket
              key={lastOperation.id}
              operation={lastOperation}
              onVoided={(op) => {
                setLastOperation(op);
                setScanMsg(`Ticket de ${formatCents(toCents(op.total_ars) ?? 0)} anulado.`);
                focusScan();
              }}
            />
          ) : null}

          <RecentTickets
            refreshKey={`${lastOperation?.id ?? ""}:${lastOperation?.status ?? ""}`}
            canVoid={canVoid}
            onReprint={(id) => {
              void printReceipt(id);
              focusScan();
            }}
            onVoided={() => {
              setScanMsg("Ticket anulado.");
              focusScan();
            }}
          />
        </aside>
      </div>

      {picker ? (
        <ProductPicker
          picker={picker}
          onPick={(p) => void onPick(p)}
          onClose={() => {
            setPicker(null);
            focusScan();
          }}
        />
      ) : null}
    </div>
  );
}
