"use client";

import { Moon, Sun } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useReducer, useRef, useState } from "react";

import { Button } from "@/components/ui/Button";
import { CartTable } from "@/features/pos/CartTable";
import { CashSessionBar } from "@/features/pos/CashSessionBar";
import { CashSessionsPanel } from "@/features/pos/CashSessionsPanel";
import { CashierHome } from "@/features/pos/CashierHome";
import { CloseCashSession } from "@/features/pos/CloseCashSession";
import { DiscountControl } from "@/features/pos/DiscountControl";
import { OpenCashSession } from "@/features/pos/OpenCashSession";
import { PaymentPanel } from "@/features/pos/PaymentPanel";
import { type PickerMode, ProductPicker } from "@/features/pos/ProductPicker";
import { PendingOperationsPanel } from "@/features/pos/PendingOperationsPanel";
import { guardarAprendizaje } from "@/lib/offline/aprendizajes";
import { StockDiscrepanciesPanel } from "@/features/products/StockDiscrepanciesPanel";
import { RecentTickets } from "@/features/pos/RecentTickets";
import { ScanInput } from "@/features/pos/ScanInput";
import type { PosReceipt } from "@/features/pos/ticketTemplate";
import { VoidLastTicket } from "@/features/pos/VoidLastTicket";
import { useCashSession } from "@/features/pos/useCashSession";
import { usePosPrinting } from "@/features/pos/usePosPrinting";
import { usePosSync } from "@/features/pos/usePosSync";
import { useScanner } from "@/features/pos/useScanner";
import {
  type ProductoDeCaja,
  catalogoDeCaja,
  nombreDelNegocio,
  sincronizarCatalogo,
  ultimaSincronizacion,
} from "@/lib/offline/catalogDb";
import { migrarPendienteDeB13 } from "@/lib/offline/migracionPendiente";
import {
  type OperacionGuardada,
  actualizarOperacion,
  borrarOperacion,
  guardarOperacion,
  pedirPersistencia,
  podarSincronizadas,
} from "@/lib/offline/posDb";
import {
  activarVersionNueva,
  desregistrarServiceWorker,
  prepararParaSinInternet,
  type EstadoSinInternet,
} from "@/lib/offline/serviceWorker";
import { tomarCandadoDeCaja } from "@/lib/offline/tabLock";
import {
  type CartLine,
  type PosOperationPayload,
  type PosOperationResult,
  type PosProduct,
  buildPayload,
  cannotCharge,
  formatCents,
  initialSale,
  esCorregibleEnElMostrador,
  newOperationId,
  previewTotals,
  saleReducer,
  toCents,
} from "@/lib/pos/cart";
import { type PosErrorInfo, cobroRejectionMessage, posErrorInfo } from "@/lib/pos/errors";
import { clasificarSync, esFalloDeRed } from "@/lib/pos/syncClassification";
import { reciboLocal } from "@/lib/pos/reciboLocal";
import { buscarEnCatalogo, clasificar } from "@/lib/pos/scanCodeLocal";
import { CASHIER_ROLE, POS_ROLES, hasPosPermission } from "@/lib/roles";
import { logoutRequest } from "@/services/auth.service";
import { type ScanCandidate, posService } from "@/services/pos.service";
import { useAuthStore } from "@/stores/authStore";
import { type PageSize, type PaperWidth, usePosPrintConfigStore } from "@/stores/posPrintConfigStore";
import { usePosScannerConfigStore } from "@/stores/posScannerConfigStore";
import { usePosSwStore } from "@/stores/posSwStore";
import { usePosTerminalStore } from "@/stores/posTerminalStore";
import { posThemeClass, usePosThemeStore } from "@/stores/posThemeStore";

const NO_SE_PUDO_GUARDAR =
  "No se pudo guardar la venta en esta PC, así que NO se cobró. Revisá el espacio del " +
  "navegador y volvé a intentar.";

const COBRADA_SIN_CONEXION =
  "Cobrada y guardada en esta PC. Se sube sola cuando vuelva la conexión.";

/**
 * Qué se le dice al cajero cuando la venta quedó en la cola.
 *
 * Si el servidor NO contestó, "cuando vuelva la conexión" es exacto. Si contestó
 * —402 porque la suscripción venció, 423 por mantenimiento, 401 o una caja dada
 * de baja—, decirle "sin conexión" lo manda a revisar el router en vez de lo que
 * de verdad hay que arreglar. La venta está igual de guardada en los dos casos.
 */
function avisoDeEncolado(info: PosErrorInfo): string {
  const guardada = "La venta quedó cobrada y guardada en esta PC.";
  switch (clasificarSync(info)) {
    case "autenticacion":
      return `${guardada} Falta volver a entrar o habilitar esta caja de nuevo; después se sube sola.`;
    case "revision":
      return `${guardada} El servidor la rechazó: la tiene que revisar el encargado. ${
        info.message ?? ""
      }`.trim();
    default:
      return info.status === null
        ? COBRADA_SIN_CONEXION
        : `${guardada} El servidor no la pudo tomar todavía; se reintenta sola. ${
            info.message ?? ""
          }`.trim();
  }
}

const OTRA_PESTANA =
  "La caja ya está abierta en otra pestaña. Cerrá esta para no cobrar dos veces lo mismo.";

/**
 * La caja (B13). Decide quién puede usarla y con qué PC; la pantalla en sí es
 * `PosRegister`.
 */
export function PosScreen() {
  const theme = usePosThemeStore((s) => s.theme);
  // El tema envuelve TODO, también el bloqueo de "PC no habilitada": si no,
  // `CashierHome` quedaría afuera y sin las variables `--pos-*` del tema.
  return (
    <div className={posThemeClass(theme)} data-testid="pos-root">
      <PosGate />
    </div>
  );
}

function PosGate() {
  const user = useAuthStore((s) => s.user);
  const terminal = usePosTerminalStore((s) => s.terminal);
  const invalid = usePosTerminalStore((s) => s.invalid);

  if (!user || !POS_ROLES.has(user.role)) {
    return (
      <div className="flex h-screen items-center justify-center bg-pos-bg p-6 text-sm text-pos-text-2">
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
  const { theme, toggle: toggleTheme } = usePosThemeStore();
  const sinInternet = usePosSwStore();
  const impresora = usePosPrintConfigStore();
  const { printReceipt, printLocal, printTest, printError } = usePosPrinting();

  const [sale, dispatch] = useReducer(saleReducer, initialSale);
  const saleRef = useRef(sale);
  saleRef.current = sale;
  const [picker, setPicker] = useState<PickerMode | null>(null);
  const [scanMsg, setScanMsg] = useState<string | null>(null);
  const [lastOperation, setLastOperation] = useState<PosOperationResult | null>(null);
  const scanRef = useRef<HTMLInputElement>(null);

  const tenantId = user?.tenant_id ?? "";
  const userId = user?.id ?? "";
  // La caja se identifica con la terminal SOLO si es de este negocio: el store
  // sobrevive a un cambio de usuario a propósito (B6).
  const terminalId =
    terminal && terminal.tenantId === tenantId ? terminal.terminalId : null;

  // ─── Sin conexión ─────────────────────────────────────────────────────────
  const [candado, setCandado] = useState<"esperando" | "propia" | "otra">("esperando");
  const [catalogo, setCatalogo] = useState<ProductoDeCaja[]>([]);
  // Por ref: `handleScan` se pasa al lector, que se registra una sola vez.
  const catalogoRef = useRef<ProductoDeCaja[]>([]);
  catalogoRef.current = catalogo;
  const [sincronizadoEn, setSincronizadoEn] = useState<string | null>(null);
  const [negocio, setNegocio] = useState<string>("");
  const [persistencia, setPersistencia] = useState<boolean | null>(null);
  // Un cobro `EN_CORRECCION` no está "sin sincronizar" —reenviarlo daría el
  // mismo rechazo—: está sin completar, que es otra cosa y se arregla distinto.
  const { pendientes, aprendizajes, sincronizando, flush, refrescar } = usePosSync({
    tenantId,
    userId,
    terminalId,
    habilitado: candado === "propia",
  });
  const sinCompletar = pendientes.filter((o) => o.estado === "EN_CORRECCION").length;
  const sinSincronizar = pendientes.length - sinCompletar;

  // El turno de esta caja (B8). Antes nacía con un `useRef` en cada montaje, así
  // que cada recarga de la pantalla abría otro turno con fondo $0 que nadie
  // cerraba. Ahora vive en `posCashSessionStore` y sobrevive a la recarga; sin
  // turno abierto no se cobra.
  const { turno, avisoDeCierre, anterioresAbiertos, abrir, confirmarApertura, soltar } =
    useCashSession({
    tenantId,
    userId,
    terminalId,
    habilitado: candado === "propia",
  });
  const turnoRef = useRef(turno);
  turnoRef.current = turno;
  const [vista, setVista] = useState<"caja" | "cerrar" | "turnos">("caja");
  const esEncargado = user?.role === "OWNER" || user?.role === "ADMIN";
  // Lo pendiente de ESTE turno, en dos grupos: lo que no subió y lo que quedó
  // en corrección (que tiene `charged_at` afirmado: puede haber plata de eso en
  // el cajón). Los dos se avisan al cerrar, rotulados aparte.
  const delTurno = turno
    ? pendientes.filter((o) => o.payload.client_session_id === turno.clientSessionId)
    : [];
  const pendientesDelTurno = {
    enCorreccion: delTurno.filter((o) => o.estado === "EN_CORRECCION").length,
    sinSincronizar: delTurno.filter((o) => o.estado !== "EN_CORRECCION").length,
  };
  const canDiscount = hasPosPermission(user, "discount");
  const canFiado = hasPosPermission(user, "fiado");
  const canLearn = hasPosPermission(user, "learn_barcode");
  const canVoid = hasPosPermission(user, "void_ticket");
  const canOverridePrice = hasPosPermission(user, "price_override");
  const canResolve = hasPosPermission(user, "resolve_discrepancy");

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
        // Una línea cobrada a mano vuelve con el precio manual: no cambió nada
        // del catálogo, y `APPLY_PRICES` tampoco la toca.
        if (antes?.manualPriceCents != null) continue;
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

  /**
   * Los cobros rechazados por algo corregible desde que se armó este carrito.
   *
   * Se borran recién cuando el cobro corregido queda registrado —entró o se
   * encoló—, que es lo que prueba que la corrección se hizo. Si el cajero se va
   * antes, sobreviven y quedan a la vista: un `charged_at` afirmado sin venta
   * que lo respalde tiene que poder revisarse.
   */
  const enCorreccion = useRef<string[]>([]);
  const cerrarCorrecciones = useCallback(async () => {
    const ids = enCorreccion.current;
    enCorreccion.current = [];
    for (const id of ids) await borrarOperacion(id);
  }, []);
  const send = useCallback(
    async (payload: PosOperationPayload, lines: CartLine[], recibo: PosReceipt) => {
      if (enVuelo.current) return;
      enVuelo.current = true;
      dispatch({ type: "SEND", payload });

      // PRIMERO se guarda, y si no se puede, NO se cobra. Hasta B7 esto era un
      // `try/catch` vacío y el cobro se mandaba igual: si el navegador se
      // cerraba con la respuesta en vuelo, la venta no quedaba en ningún lado.
      try {
        await guardarOperacion({
          clientOperationId: payload.client_operation_id,
          tenantId,
          userId,
          terminalId,
          payload,
          lines,
          recibo,
        });
      } catch {
        enVuelo.current = false;
        dispatch({ type: "REJECTED", notice: NO_SE_PUDO_GUARDAR });
        focusScan();
        return;
      }

      try {
        const result = await posService.createOperation(payload);
        await cerrarCorrecciones();
        await actualizarOperacion(payload.client_operation_id, {
          estado: "SINCRONIZADA",
          resultado: result,
          sincronizadaEn: new Date().toISOString(),
        });
        dispatch({ type: "SUCCESS", result });
        setLastOperation(result);
        // Recién ahora, con la venta confirmada: una impresión que falla no
        // pone en duda el cobro.
        if (usePosPrintConfigStore.getState().autoPrint) void printReceipt(result.id);
      } catch (e) {
        const info = posErrorInfo(e);
        if (esCorregibleEnElMostrador(info)) {
          // El servidor revirtió todo y el cajero puede arreglar el carrito y
          // volver a cobrar. **Pero no se borra**: al apretar Cobrar ya se
          // afirmó `charged_at`, así que puede haber plata recibida, y si el
          // cajero se va o se cierra la pestaña antes de rehacer el cobro eso
          // quedaría sin constancia. Queda `EN_CORRECCION`, a la vista y sin
          // sincronizarse, y se borra sola cuando el cobro corregido entra.
          await actualizarOperacion(payload.client_operation_id, {
            estado: "EN_CORRECCION",
            ultimoError: info.message ?? "El servidor la rechazó",
            ultimoCodigo: info.code,
          });
          enCorreccion.current.push(payload.client_operation_id);
          handleRejection(info, lines);
        } else {
          // No se sabe si entró. La venta ya está guardada: se libera el
          // carrito, sale el comprobante provisional y la cola la sube sola.
          await actualizarOperacion(payload.client_operation_id, {
            intentos: 1,
            ultimoError: info.message ?? "Sin respuesta del servidor",
            ultimoCodigo: info.code,
            proximoIntentoEn: Date.now() + 5_000,
          });
          // La venta quedó registrada (en la cola): los intentos rechazados
          // que la precedieron ya están corregidos por ésta.
          await cerrarCorrecciones();
          dispatch({ type: "QUEUED", notice: avisoDeEncolado(info) });
          if (usePosPrintConfigStore.getState().autoPrint) void printLocal(recibo);
        }
      } finally {
        enVuelo.current = false;
        void refrescar();
        focusScan();
      }
    },
    [
      tenantId,
      userId,
      terminalId,
      handleRejection,
      focusScan,
      printReceipt,
      printLocal,
      refrescar,
    ],
  );

  const cobrar = useCallback(() => {
    const s = saleRef.current;
    if (s.status !== "editando") return;
    const turnoActual = turnoRef.current;
    if (!turnoActual) {
      dispatch({ type: "NOTICE", notice: "Abrí el turno antes de cobrar." });
      return;
    }
    const motivo = cannotCharge(s);
    if (motivo) {
      dispatch({ type: "NOTICE", notice: motivo });
      return;
    }
    // Id, fecha y turno nacen acá, una sola vez: el reintento reenvía este
    // mismo objeto, y recalcular la fecha cambiaría la huella de idempotencia.
    const payload = buildPayload(s, newOperationId(), new Date(), turnoActual.clientSessionId);
    const recibo = reciboLocal(payload, s.lines, {
      businessName: negocio,
      cashierName: user?.full_name ?? null,
      terminalName: terminal?.name ?? null,
      customerName: s.customerName,
    });
    void send(payload, s.lines, recibo);
  }, [send, negocio, user, terminal]);

  // ─── Arranque de la caja ──────────────────────────────────────────────────

  // Una sola pestaña por PC: dos cajas sobre la misma cola se pisan el estado.
  useEffect(() => {
    let soltar = () => {};
    let vivo = true;
    void (async () => {
      const c = await tomarCandadoDeCaja();
      soltar = c.soltar;
      if (vivo) setCandado(c.obtenido ? "propia" : "otra");
    })();
    return () => {
      vivo = false;
      soltar();
    };
  }, []);

  const bajarCatalogo = useCallback(async () => {
    if (!tenantId) return;
    try {
      await sincronizarCatalogo(tenantId, (after) => posService.catalogPage(after));
    } catch {
      // Sin red se sigue con el catálogo guardado: viejo, pero completo.
    }
    setCatalogo(await catalogoDeCaja(tenantId));
    setSincronizadoEn(await ultimaSincronizacion(tenantId));
    setNegocio((await nombreDelNegocio(tenantId)) ?? "");
  }, [tenantId]);

  useEffect(() => {
    if (candado !== "propia" || !tenantId) return;
    void bajarCatalogo();
    const alVolver = () => void bajarCatalogo();
    window.addEventListener("online", alVolver);
    const timer = window.setInterval(() => void bajarCatalogo(), 15 * 60_000);
    return () => {
      window.removeEventListener("online", alVolver);
      window.clearInterval(timer);
    };
  }, [candado, tenantId, bajarCatalogo]);

  // El stock estimado se recalcula cuando cambia la cola: es DERIVADO de los
  // pendientes, nunca acumulado sobre el producto guardado.
  // Lo mismo con los códigos aprendidos sin red: se superponen al leer.
  useEffect(() => {
    if (!tenantId) return;
    void catalogoDeCaja(tenantId).then(setCatalogo);
  }, [tenantId, pendientes, aprendizajes]);

  // El service worker se registra SÓLO si el dueño encendió la casilla en esta
  // PC, y apagarla lo desregistra y borra su caché. Una PC que nunca lo
  // encendió no lo tiene: el radio de daño es una caja, no la app entera.
  //
  // Y "registrado" no es "lista": los chunks que esta pantalla ya tenía
  // cargados no pasaron por el service worker, así que hay que pasárselos y
  // esperar su confirmación antes de decirle al dueño que puede reiniciar sin
  // red. Decirlo antes es peor que no instalar nada.
  const [preparada, setPreparada] = useState<EstadoSinInternet | "preparando" | null>(null);
  useEffect(() => {
    if (!sinInternet.habilitado) {
      setPreparada(null);
      void desregistrarServiceWorker();
      return;
    }
    let vigente = true;
    setPreparada("preparando");
    void prepararParaSinInternet().then((estado) => {
      if (vigente) setPreparada(estado);
    });
    return () => {
      vigente = false;
    };
  }, [sinInternet.habilitado]);

  // Una versión nueva se activa en un momento SEGURO: con el carrito vacío y
  // sin nada en vuelo. `skipWaiting` a secas mezclaría código nuevo con una
  // página vieja, y acá eso puede pasar en medio de un cobro.
  useEffect(() => {
    if (!sinInternet.habilitado) return;
    if (sale.status !== "editando" || sale.lines.length > 0 || sincronizando) return;
    void activarVersionNueva();
  }, [sinInternet.habilitado, sale.status, sale.lines.length, sincronizando]);

  const arrancado = useRef(false);
  useEffect(() => {
    if (arrancado.current || candado !== "propia" || !tenantId || !user) return;
    arrancado.current = true;
    void (async () => {
      setPersistencia(await pedirPersistencia());
      // Lo ya subido vive en el servidor: acá sólo se conserva unos días para
      // reimprimir. Sin poda, la base crece hasta que el navegador la evacúa —
      // y eso se llevaría puestas las PENDIENTES.
      await podarSincronizadas(tenantId);
      // Puente de una sola vez: la venta que B13 dejaba en `localStorage`.
      await migrarPendienteDeB13({
        tenantId,
        userId,
        terminalId,
        businessName: negocio,
        cashierName: user.full_name ?? null,
        terminalName: terminal?.name ?? null,
      });
      await refrescar();
      await flush();
    })();
  }, [candado, tenantId, user, userId, terminalId, negocio, terminal, refrescar, flush]);

  // ─── Scan ─────────────────────────────────────────────────────────────────

  const handleScan = useCallback(
    async (raw: string) => {
      const code = raw.trim();
      if (!code) return;
      if (!turnoRef.current) {
        setScanMsg("Abrí el turno antes de escanear.");
        return;
      }
      const s = saleRef.current;
      if (s.status === "enviando") {
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
          // Sin red se busca en el catálogo de la PC, con la MISMA clasificación
          // que usa el servidor (`lib/pos/scanCodeLocal`, probada contra el
          // corpus compartido). Si fuera más laxa, la caja podría cobrar un
          // producto que el servidor habría marcado como ambiguo.
          const hallados = buscarEnCatalogo(clasificar(code), catalogoRef.current);
          if (hallados.length === 1) {
            const p = hallados[0]!.producto;
            // Se muestra el stock ESTIMADO: el del servidor menos lo que la
            // caja ya cobró y todavía no subió.
            dispatch({ type: "ADD_PRODUCT", product: { ...p, stock_units: p.stock_estimado } });
          } else if (hallados.length > 1) {
            setPicker({
              mode: "ambiguous",
              code,
              candidates: hallados.map((h) => h.producto),
            });
          } else if (canLearn && clasificar(code).aprendible) {
            // Sin red también se puede aprender: queda guardado en la PC y
            // sube cuando vuelve la conexión (ver `lib/offline/aprendizajes`).
            setPicker({ mode: "learn", code });
          } else {
            setScanMsg(
              `Sin conexión y «${code}» no está en el catálogo guardado en esta PC.`,
            );
          }
        } else {
          setScanMsg(info.message ?? `No se pudo buscar «${code}».`);
        }
      }
    },
    [canLearn],
  );

  useScanner({
    onScan: (c) => void handleScan(c),
    // Sólo en la caja con turno: en el cierre o en el panel de turnos una
    // lectura agregaba un producto a un carrito que no se ve, y el turno se
    // firmaba con una línea colgada.
    enabled: picker === null && vista === "caja" && turno !== null,
    terminator,
    thresholdMs,
  });

  /**
   * Aprender un código sin red. Las MISMAS precondiciones que el servidor,
   * antes de guardar: si no, la caja vendería con un vínculo que al sincronizar
   * se rechaza. Devuelve el motivo si no se puede, o `null` si quedó guardado.
   */
  const aprenderSinRed = useCallback(
    async (code: string, product: PosProduct): Promise<string | null> => {
      const codigo = clasificar(code);
      if (!codigo.aprendible) return `«${code}» no es un código que se pueda vincular.`;
      const actual = catalogoRef.current.find((p) => p.id === product.id);
      if ((actual?.barcode ?? product.barcode)?.trim()) {
        return `${product.name} ya tiene otro código cargado: no se vinculó «${code}».`;
      }
      if (buscarEnCatalogo(codigo, catalogoRef.current).length > 0) {
        return `«${code}» ya es de otro producto. Avisale al encargado.`;
      }
      try {
        await guardarAprendizaje({
          tenantId,
          userId,
          terminalId,
          codigo: codigo.valor,
          productId: product.id,
          productName: product.name,
        });
      } catch {
        return "No se pudo guardar el código en esta PC.";
      }
      await refrescar();
      return null;
    },
    [tenantId, userId, terminalId, refrescar],
  );

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
        if (esFalloDeRed(info)) {
          const motivo = await aprenderSinRed(actual.code, product);
          if (motivo === null) {
            dispatch({ type: "ADD_PRODUCT", product });
            setScanMsg(
              `«${actual.code}» quedó vinculado a ${product.name} en esta PC. ` +
                "Se sube cuando vuelva la conexión.",
            );
          } else {
            setScanMsg(motivo);
          }
          return;
        }
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
    [picker, focusScan, aprenderSinRed],
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

  // Después de todos los hooks: dos pestañas sobre la misma cola se pisan el
  // estado y el cajero ve "pendiente" algo que ya está sincronizado.
  if (candado === "otra") {
    return (
      <div className="flex h-screen items-center justify-center bg-pos-bg p-6 text-center text-sm text-pos-text-2">
        {OTRA_PESTANA}
      </div>
    );
  }

  return (
    // Color de texto en la raíz: el `body` de la app hereda un color CLARO, y
    // todo lo que no declaraba color —los menús de Impresora y Lector, sus
    // selects— quedaba blanco sobre blanco. Los `pos-*` siguen al tema.
    <div className="flex h-screen flex-col bg-pos-bg text-pos-text">
      <header className="flex items-center justify-between border-b border-pos-border bg-pos-surface px-4 py-2">
        <div className="text-sm">
          <span className="font-semibold text-pos-text">Caja</span>
          <span className="ml-2 text-pos-muted">
            {terminal && terminal.tenantId === tenantId && !terminalInvalid
              ? terminal.name
              : "navegador (sin caja habilitada)"}
            {" · "}
            {user?.full_name}
          </span>
          {/* El stock que muestra la caja sale del catálogo guardado menos lo
              que ya se cobró y no subió: es una estimación y se dice que lo es. */}
          <span className="ml-2 text-pos-muted" data-testid="pos-sync">
            {sincronizadoEn
              ? `· stock estimado — última sinc. ${new Date(sincronizadoEn).toLocaleTimeString(
                  "es-AR",
                  { hour: "2-digit", minute: "2-digit" },
                )}`
              : "· sin catálogo guardado todavía"}
            {sinSincronizar > 0 ? ` · ${sinSincronizar} sin sincronizar` : ""}
            {sinCompletar > 0 ? ` · ${sinCompletar} sin completar` : ""}
          </span>
          {user?.role === "OWNER" &&
          !(terminal && terminal.tenantId === tenantId && !terminalInvalid) ? (
            <Link
              href="/settings?tab=seguridad"
              className="ml-3 text-pos-link underline hover:text-pos-link/80"
            >
              Habilitar esta PC como caja
            </Link>
          ) : null}
        </div>
        <div className="flex items-center gap-3 text-sm">
          <button
            type="button"
            className="flex items-center gap-1 text-pos-text-2 hover:text-pos-text"
            aria-label={theme === "claro" ? "Pasar a tema oscuro" : "Pasar a tema claro"}
            onClick={() => {
              toggleTheme();
              focusScan();
            }}
          >
            {theme === "claro" ? <Moon className="h-4 w-4" /> : <Sun className="h-4 w-4" />}
            {theme === "claro" ? "Oscuro" : "Claro"}
          </button>
          <details className="relative">
            <summary className="cursor-pointer text-pos-text-2">Impresora</summary>
            <div className="absolute right-0 z-10 mt-2 w-72 space-y-2 rounded-lg border border-pos-border bg-pos-surface p-3 shadow-vk-lg">
              <label className="flex items-center justify-between gap-2">
                Papel
                <select
                  aria-label="Ancho del papel"
                  value={impresora.paperWidth}
                  onChange={(e) => impresora.setPaperWidth(Number(e.target.value) as PaperWidth)}
                  className="rounded border border-pos-border bg-pos-field px-1 text-pos-text"
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
                  className="rounded border border-pos-border bg-pos-field px-1 text-pos-text"
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
            <summary className="cursor-pointer text-pos-text-2">Lector</summary>
            <div className="absolute right-0 z-10 mt-2 w-64 space-y-2 rounded-lg border border-pos-border bg-pos-surface p-3 shadow-vk-lg">
              <label className="flex items-center justify-between gap-2">
                Termina con
                <select
                  aria-label="Terminador del lector"
                  value={terminator}
                  onChange={(e) => setTerminator(e.target.value as "Enter" | "Tab")}
                  className="rounded border border-pos-border bg-pos-field px-1 text-pos-text"
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
                  className="w-20 rounded border border-pos-border bg-pos-field px-1 text-right text-pos-text"
                />
              </label>
            </div>
          </details>
          {user?.role === "OWNER" ? (
            <details className="relative">
              <summary className="cursor-pointer text-pos-text-2">Sin internet</summary>
              <div className="absolute right-0 z-10 mt-2 w-72 space-y-2 rounded-lg border border-pos-border bg-pos-surface p-3 shadow-vk-lg">
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    aria-label="Preparar esta PC para abrir sin internet"
                    checked={sinInternet.habilitado}
                    onChange={(e) => sinInternet.setHabilitado(e.target.checked)}
                  />
                  Preparar esta PC para abrir sin internet
                </label>
                {preparada === "preparando" ? (
                  <p className="text-xs text-pos-muted">Guardando la caja en esta PC…</p>
                ) : null}
                {preparada && preparada !== "preparando" ? (
                  <p className={`text-xs ${preparada.lista ? "text-pos-text-2" : "text-pos-danger"}`}>
                    {preparada.lista
                      ? "Lista: esta PC ya puede abrir la caja sin conexión."
                      : `Todavía NO está lista${
                          preparada.faltan.length
                            ? ` (faltan ${preparada.faltan.length} archivos)`
                            : ""
                        }. Recargá la página con conexión y volvé a probar.`}
                  </p>
                ) : null}
                <p className="text-xs text-pos-muted">
                  Guarda la caja en esta PC para que abra aunque no haya conexión. Probalo
                  cortando internet y reiniciando antes de confiar en él; si algo anda mal,
                  destildá y la PC vuelve como estaba.
                </p>
              </div>
            </details>
          ) : null}
          {user?.role !== CASHIER_ROLE ? (
            <Link href="/dashboard" className="text-pos-text-2 hover:text-pos-text">
              Volver a Véktor
            </Link>
          ) : null}
          <button type="button" className="text-pos-text-2" onClick={() => void salir()}>
            Cerrar sesión
          </button>
        </div>
      </header>

      {turno && vista !== "turnos" ? (
        <CashSessionBar
          turno={turno}
          onCerrar={() => {
            const actual = saleRef.current;
            if (actual.status === "enviando") {
              setScanMsg("Esperá a que termine el cobro en curso antes de cerrar el turno.");
              return;
            }
            // Una venta ya cobrada conserva sus líneas en el estado (para el
            // vuelto y la reimpresión): no es un carrito pendiente.
            if (actual.status === "cobrada") dispatch({ type: "NEW_SALE" });
            else if (actual.lines.length > 0) {
              setScanMsg("Cobrá o vaciá el carrito antes de cerrar el turno.");
              return;
            }
            setVista("cerrar");
          }}
          onVerTurnos={esEncargado ? () => setVista("turnos") : undefined}
        />
      ) : null}

      {vista === "turnos" && esEncargado ? (
        <CashSessionsPanel
          turnoActual={turno?.clientSessionId ?? null}
          terminalActual={terminalId}
          onVolver={() => {
            setVista("caja");
            focusScan();
          }}
        />
      ) : !turno ? (
        <div className="flex flex-1 flex-col">
          <OpenCashSession
            aviso={avisoDeCierre}
            anterioresAbiertos={anterioresAbiertos}
            onAbrir={async (fondo) => {
              await abrir(fondo);
              setVista("caja");
              focusScan();
            }}
          />
          {esEncargado ? (
            <div className="pb-6 text-center text-sm">
              <button
                type="button"
                className="text-pos-link underline"
                onClick={() => setVista("turnos")}
              >
                Ver los turnos de todas las cajas
              </button>
            </div>
          ) : null}
        </div>
      ) : vista === "cerrar" ? (
        <CloseCashSession
          turno={turno}
          pendientes={pendientesDelTurno}
          confirmarApertura={confirmarApertura}
          onSincronizar={async () => {
            await confirmarApertura();
            await flush();
            await refrescar();
          }}
          onTerminado={() => {
            soltar();
            setVista("caja");
            setLastOperation(null);
          }}
          onCancelar={() => {
            setVista("caja");
            focusScan();
          }}
        />
      ) : (
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
              canOverridePrice={canOverridePrice}
              onAfterAction={focusScan}
            />
          </section>

          <aside className="flex w-full flex-col gap-4 rounded-xl border border-pos-border bg-pos-surface p-4 lg:w-96">
            {sale.status === "cobrada" && sale.result ? (
              <div className="space-y-3" data-testid="venta-cobrada">
                <p className="text-sm text-vk-success">Venta cobrada</p>
                <p className="text-3xl font-semibold text-pos-text">
                  {formatCents(toCents(sale.result.total_ars) ?? 0)}
                </p>
                {sale.result.cash_change_ars !== null ? (
                  <p className="text-lg text-pos-text">
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
                  <span className="text-sm text-pos-text-2">Total</span>
                  <span className="text-3xl font-semibold text-pos-text" data-testid="total">
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
                  <p role="alert" className="text-sm text-pos-danger">
                    {sale.notice}
                  </p>
                ) : null}

                <Button
                  className="w-full"
                  size="lg"
                  loading={sale.status === "enviando"}
                  disabled={sale.status !== "editando" || sale.lines.length === 0}
                  onClick={cobrar}
                >
                  Cobrar {formatCents(totalCents)}
                </Button>
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

            <PendingOperationsPanel
              pendientes={pendientes}
              aprendizajes={aprendizajes}
              terminalId={terminalId}
              esDuenio={user?.role === "OWNER"}
              sincronizando={sincronizando}
              persistencia={persistencia}
              onFlush={() => void flush()}
              onCambio={() => void refrescar()}
              onImprimir={(op: OperacionGuardada) => {
                void printLocal(op.recibo);
                focusScan();
              }}
            />

            {/* El cajero no llega a /products: si el dueño le delegó resolver
                diferencias de stock, las resuelve acá, con su PIN. El dueño y
                el encargado las siguen resolviendo en /products. */}
            {canResolve && user?.role === CASHIER_ROLE ? <StockDiscrepanciesPanel enCaja /> : null}

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
      )}

      {picker ? (
        <ProductPicker
          picker={picker}
          local={catalogo}
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
