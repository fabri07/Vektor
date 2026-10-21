/**
 * La venta de la caja (B13), sin React: carrito, vista previa y estados.
 *
 * Todo en CENTAVOS ENTEROS. Con floats, $0,10 + $0,20 no es $0,30 y la vista
 * previa dejaría de coincidir con el total del servidor —que reparte en
 * enteros— justo en el centavo que hace que el pago no cuadre.
 *
 * La vista previa es sólo eso: el precio lo fija el servidor desde el catálogo
 * (B3). Después de cobrar se muestra lo que el servidor devolvió.
 */

import type { PosReceipt } from "@/features/pos/ticketTemplate";
import type { PosErrorInfo } from "@/lib/pos/errors";
import { clasificarSync } from "@/lib/pos/syncClassification";

export interface PosProduct {
  id: string;
  name: string;
  internal_sku?: string | null;
  sku?: string | null;
  barcode?: string | null;
  sale_price_ars: string;
  sale_unit: string;
  base_units_per_sale_unit: number;
  stock_units: number;
  stock_display?: string;
  /**
   * La firma del servidor sobre precio + unidad + factor. La caja la manda de
   * vuelta al cobrar: es lo que le permite declarar el precio que cobró sin que
   * el servidor tenga que confiar en ella. `null` en un producto que quedó
   * guardado antes de B7·1 — ahí manda el catálogo, como siempre.
   */
  price_token?: string | null;
  /**
   * B·5e-2: hoy hay compra o stock inicial con costo (viene en el catálogo).
   * `false` = no se puede vender («no se vende lo que no se compró»), también
   * sin red. `null`/ausente = no se sabe (lookup en vivo: decide el servidor).
   */
  tiene_compra?: boolean | null;
}

export interface CartLine {
  product: PosProduct;
  /** En UNIDADES BASE (gramos, mililitros o unidades), como el servidor. */
  quantity: number;
  discountCents: number;
  /**
   * Precio puesto A MANO (permiso `price_override`), por unidad de venta. Va
   * aparte del precio firmado: el servidor sigue verificando el de lista con
   * su firma y cobra éste. Ausente = el de lista.
   */
  manualPriceCents?: number | null;
  /** Obligatorio con precio manual: el servidor lo exige. */
  manualPriceReason?: string;
}

export interface PosOperationPayload {
  client_operation_id: string;
  customer_id: string | null;
  operation_date: string;
  discount_ars: string;
  cash_received_ars: string | null;
  /** Cuándo el cajero cerró el ticket. Distinto de la fecha de negocio. */
  charged_at: string | null;
  /** El turno de caja, para que el arqueo pueda atribuir lo que llega tarde. */
  client_session_id: string | null;
  items: {
    product_id: string;
    quantity: number;
    discount_ars: string;
    /** Precio, nombre, unidad, factor y firma: los cinco juntos o ninguno. */
    unit_price_ars?: string;
    product_name?: string;
    sale_unit?: string;
    base_units_per_sale_unit?: number;
    price_token?: string;
    /** Sólo si se cobró a mano: así una venta sin cambio de precio conserva
     *  exactamente la huella de siempre. */
    manual_unit_price_ars?: string;
    manual_price_reason?: string;
  }[];
  tenders: { payment_method: string; amount_ars: string }[];
}

export interface PosOperationResult {
  id: string;
  client_operation_id: string;
  total_ars: string;
  subtotal_ars: string;
  discount_ars: string;
  cash_received_ars: string | null;
  cash_change_ars: string | null;
  status: string;
}

/**
 * - `editando`: el carrito se puede tocar.
 * - `enviando`: hay un cobro en vuelo; nada se edita ni se reenvía.
 * - `cobrada`: el servidor confirmó.
 *
 * **`en_duda` ya no existe.** Hasta B7, una respuesta perdida dejaba el
 * carrito bloqueado hasta que el cajero resolviera el cobro a mano: la caja se
 * trababa justo cuando más falta hacía. Ahora la venta ya está guardada en la
 * PC antes de enviarse, así que si no hay respuesta pasa a la cola, se sube
 * sola y el carrito se libera para el cliente siguiente.
 */
export type SaleStatus = "editando" | "enviando" | "cobrada";

/**
 * Un pago de la venta (B9). Con UNA sola fila, su importe se ignora y el pago
 * sigue al total: es la caja de siempre. Con varias —«Dividir pago»— cada
 * importe es explícito y nadie lo redistribuye: si después cambia el total,
 * la pantalla lo dice y Cobrar queda bloqueado hasta corregirlo.
 */
export interface PaymentRow {
  id: string;
  method: string;
  /** Lo que escribió el cajero. Sólo cuenta con más de una fila. */
  amount: string;
}

export interface SaleState {
  status: SaleStatus;
  lines: CartLine[];
  globalDiscountCents: number;
  /** Siempre al menos una. */
  payments: PaymentRow[];
  /** Lo que escribió el cajero; vacío = no informado. Sólo con un pago en efectivo. */
  cashReceived: string;
  customerId: string | null;
  customerName: string | null;
  /** El cuerpo EXACTO que se mandó. El reintento reenvía este objeto, no uno nuevo. */
  payload: PosOperationPayload | null;
  result: PosOperationResult | null;
  /** Último rechazo o aviso, para mostrar. */
  notice: string | null;
  /**
   * `aviso` = la venta se cobró y quedó en la cola: no es un error del cajero,
   * pero tampoco está en el servidor todavía (ámbar, no rojo ni verde).
   */
  noticeTono: "error" | "aviso";
  /**
   * La venta cobrada que quedó en la cola sin confirmar. Con esto la pantalla
   * muestra el mismo cierre que una venta confirmada —total, vuelto,
   * comprobante— con el sello de pendiente. Hasta 2026-10 el carrito volvía a
   * $0 con un aviso chico, y sin red el cajero no sabía si había cobrado.
   */
  guardada: PosReceipt | null;
}

export const initialSale: SaleState = {
  status: "editando",
  lines: [],
  globalDiscountCents: 0,
  payments: [{ id: "p0", method: "cash", amount: "" }],
  cashReceived: "",
  customerId: null,
  customerName: null,
  payload: null,
  result: null,
  notice: null,
  noticeTono: "error",
  guardada: null,
};

// ─── Montos ──────────────────────────────────────────────────────────────────

/** "1234.56" / "1234,56" / 1234.56 → 123456. `null` si no es un monto válido. */
export function toCents(value: string | number): number | null {
  const text = String(value).trim().replace(",", ".");
  if (!/^\d+(\.\d{0,2})?$/.test(text)) return null;
  const [entero, decimales = ""] = text.split(".");
  return Number(entero) * 100 + Number(decimales.padEnd(2, "0"));
}

/** 123456 → "1234.56", el formato que espera el servidor. */
export function centsToAmount(cents: number): string {
  const signo = cents < 0 ? "-" : "";
  const abs = Math.abs(cents);
  return `${signo}${Math.floor(abs / 100)}.${String(abs % 100).padStart(2, "0")}`;
}

/** 123456 → "$ 1.234,56". */
export function formatCents(cents: number): string {
  return new Intl.NumberFormat("es-AR", {
    style: "currency",
    currency: "ARS",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(cents / 100);
}

/**
 * precio × cantidad / factor, redondeado UNA vez, half-up. Espejo exacto de
 * `importe_bruto_en_centavos` (backend/app/domain/pos_operation.py).
 */
export function lineGrossCents(priceCents: number, quantity: number, factor: number): number {
  const f = factor > 0 ? factor : 1;
  const numerador = priceCents * quantity;
  return Math.floor((2 * numerador + f) / (2 * f));
}

/** El precio que se cobra: el manual si lo hay, si no el de lista. */
export function precioEfectivoCents(line: CartLine): number {
  return line.manualPriceCents ?? toCents(line.product.sale_price_ars) ?? 0;
}

export function lineNetCents(line: CartLine): number {
  const precio = precioEfectivoCents(line);
  return (
    lineGrossCents(precio, line.quantity, line.product.base_units_per_sale_unit) -
    line.discountCents
  );
}

export interface Totals {
  subtotalCents: number;
  totalCents: number;
}

export function previewTotals(state: Pick<SaleState, "lines" | "globalDiscountCents">): Totals {
  const subtotalCents = state.lines.reduce((acc, l) => acc + lineNetCents(l), 0);
  return { subtotalCents, totalCents: subtotalCents - state.globalDiscountCents };
}

// ─── Pagos (B9) ──────────────────────────────────────────────────────────────

/**
 * Los medios que ofrece la caja, en orden. Lista EXPLÍCITA a propósito: derivar
 * las opciones de un mapa de etiquetas ofrecería cualquier cosa que alguien le
 * agregue para MOSTRAR (como `mixed`, que es un marcador del servidor).
 */
export const POS_PAYMENT_METHODS = [
  "cash",
  "debit_card",
  "credit_card",
  "transfer",
  "qr",
  "account",
  "other",
] as const;

export const FIADO_METHOD = "account";
export const CASH_METHOD = "cash";

const PAGO_INICIAL: PaymentRow = { id: "p0", method: CASH_METHOD, amount: "" };

/** La fila de un pago único. `payments` nunca está vacío; el respaldo es para el tipo. */
function primerPago(payments: PaymentRow[]): PaymentRow {
  return payments[0] ?? PAGO_INICIAL;
}

/** Varias filas = pago dividido; los importes son del cajero, no del total. */
export function isSplit(state: Pick<SaleState, "payments">): boolean {
  return state.payments.length > 1;
}

/** El importe de cada pago en centavos. `null` = el cajero todavía no escribió uno válido. */
export function tenderCents(
  state: Pick<SaleState, "payments">,
  totalCents: number,
): { method: string; cents: number | null }[] {
  if (!isSplit(state)) return [{ method: primerPago(state.payments).method, cents: totalCents }];
  return state.payments.map((p) => ({
    method: p.method,
    cents: p.amount.trim() === "" ? null : toCents(p.amount),
  }));
}

/**
 * total − Σ pagos: positivo = falta asignar, negativo = sobra. Un importe
 * todavía VACÍO cuenta como $0 —es lo que falta asignar—; uno ilegible da
 * `null`, porque ahí no se sabe cuánto falta.
 */
export function unassignedCents(
  state: Pick<SaleState, "payments">,
  totalCents: number,
): number | null {
  if (!isSplit(state)) return 0;
  let asignado = 0;
  for (const p of state.payments) {
    if (p.amount.trim() === "") continue;
    const cents = toCents(p.amount);
    if (cents === null) return null;
    asignado += cents;
  }
  return totalCents - asignado;
}

/** La parte en efectivo: contra ESTO se calcula el vuelto, no contra el total. */
export function cashPartCents(state: Pick<SaleState, "payments">, totalCents: number): number {
  return tenderCents(state, totalCents)
    .filter((t) => t.method === CASH_METHOD)
    .reduce((acc, t) => acc + (t.cents ?? 0), 0);
}

export function hasCash(state: Pick<SaleState, "payments">): boolean {
  return state.payments.some((p) => p.method === CASH_METHOD);
}

export function hasFiado(state: Pick<SaleState, "payments">): boolean {
  return state.payments.some((p) => p.method === FIADO_METHOD);
}

/** Lo que impide cobrar, o `null` si se puede. Evita mandar lo que el servidor rechazaría seguro. */
export function cannotCharge(state: SaleState): string | null {
  if (state.lines.length === 0) return "El carrito está vacío.";
  for (const l of state.lines) {
    if (lineNetCents(l) < 0) return `El descuento de «${l.product.name}» supera su importe.`;
  }
  const { totalCents } = previewTotals(state);
  if (totalCents < 0) return "El descuento supera el total de la venta.";
  if (totalCents === 0) return "Una venta en $0 no se puede cobrar.";
  if (isSplit(state)) {
    for (const t of tenderCents(state, totalCents)) {
      if (t.cents === null) return "Hay un pago sin importe válido.";
      if (t.cents <= 0) return "Cada pago tiene que ser mayor que $0.";
    }
    const resto = unassignedCents(state, totalCents) ?? 0;
    if (resto > 0) return `Falta asignar ${formatCents(resto)} entre los pagos.`;
    if (resto < 0) return `Los pagos suman ${formatCents(-resto)} de más.`;
  }
  if (hasFiado(state) && !state.customerId) {
    return "Para vender fiado hay que elegir el cliente.";
  }
  if (hasCash(state) && state.cashReceived.trim() !== "") {
    const recibido = toCents(state.cashReceived);
    if (recibido === null) return "El efectivo recibido no es un monto válido.";
    if (recibido < cashPartCents(state, totalCents)) {
      return isSplit(state)
        ? "El efectivo recibido no alcanza para la parte en efectivo."
        : "El efectivo recibido no alcanza para el total.";
    }
  }
  return null;
}

// ─── Payload ─────────────────────────────────────────────────────────────────

/** Hora local de la PC SIN zona: la fecha de negocio es la que ve el cajero (B0). */
export function localNaiveIso(d: Date): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}` +
    `T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
  );
}

export function newOperationId(): string {
  const c = globalThis.crypto as Crypto | undefined;
  if (c && typeof c.randomUUID === "function") return c.randomUUID();
  return `op-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

/**
 * Congela la venta en el cuerpo que se va a mandar. Se llama UNA vez, al
 * apretar Cobrar: el id y la fecha nacen acá y el reintento reenvía este mismo
 * objeto. Recalcular la fecha en el reintento cambiaría la huella de
 * idempotencia y el servidor contestaría `IDEMPOTENCY_KEY_REUSED` aunque la
 * venta exista.
 */
export function buildPayload(
  state: SaleState,
  id: string,
  now: Date,
  sessionId: string | null = null,
): PosOperationPayload {
  const { totalCents } = previewTotals(state);
  const efectivo = hasCash(state) && state.cashReceived.trim() !== "";
  const fecha = localNaiveIso(now);
  return {
    client_operation_id: id,
    customer_id: hasFiado(state) ? state.customerId : null,
    operation_date: fecha,
    discount_ars: centsToAmount(state.globalDiscountCents),
    cash_received_ars: efectivo ? centsToAmount(toCents(state.cashReceived) ?? 0) : null,
    // Cuándo se cobró de verdad. Una venta que sube dos días después sigue
    // diciendo cuándo entró la plata, y es lo que habilita registrar una salida
    // pendiente de stock en vez de perder la venta.
    charged_at: fecha,
    client_session_id: sessionId,
    items: state.lines.map((l) => ({
      product_id: l.product.id,
      quantity: l.quantity,
      discount_ars: centsToAmount(l.discountCents),
      // El precio que la caja COBRÓ, con la firma que emitió el servidor. Sin
      // esto, una venta sincronizada después de un cambio de precio se
      // recalcularía con el precio nuevo y los pagos no cerrarían.
      // El nombre y la unidad van con el precio y la firma: son lo que el
      // ticket que el cliente se llevó dice que se vendió. Leerlos del catálogo
      // al sincronizar hacía que un producto renombrado durante el corte
      // apareciera con otro nombre en el comprobante definitivo.
      ...(l.product.price_token
        ? {
            unit_price_ars: l.product.sale_price_ars,
            product_name: l.product.name,
            sale_unit: l.product.sale_unit,
            base_units_per_sale_unit: l.product.base_units_per_sale_unit,
            price_token: l.product.price_token,
          }
        : {}),
      ...(l.manualPriceCents != null
        ? {
            manual_unit_price_ars: centsToAmount(l.manualPriceCents),
            manual_price_reason: l.manualPriceReason ?? "",
          }
        : {}),
    })),
    // Con un solo pago, el mismo JSON de siempre: un tender por el total.
    tenders: tenderCents(state, totalCents).map((t) => ({
      payment_method: t.method,
      amount_ars: centsToAmount(t.cents ?? 0),
    })),
  };
}

// ─── Reducer ─────────────────────────────────────────────────────────────────

export type SaleAction =
  | { type: "ADD_PRODUCT"; product: PosProduct }
  | { type: "SET_QUANTITY"; productId: string; quantity: number }
  | { type: "REMOVE"; productId: string }
  | { type: "SET_LINE_DISCOUNT"; productId: string; cents: number }
  /** `cents: null` vuelve al precio de lista. */
  | { type: "SET_LINE_PRICE"; productId: string; cents: number | null; reason: string }
  | { type: "SET_GLOBAL_DISCOUNT"; cents: number }
  | { type: "SET_PAYMENT_METHOD"; id: string; method: string }
  | { type: "SET_PAYMENT_AMOUNT"; id: string; value: string }
  | { type: "SPLIT_PAYMENT" }
  | { type: "REMOVE_PAYMENT"; id: string }
  | { type: "SET_CASH_RECEIVED"; value: string }
  | { type: "SET_CUSTOMER"; id: string | null; name: string | null }
  | { type: "SEND"; payload: PosOperationPayload }
  | { type: "SUCCESS"; result: PosOperationResult }
  | { type: "REJECTED"; notice: string }
  | { type: "QUEUED"; notice: string; recibo: PosReceipt }
  | { type: "APPLY_PRICES"; prices: Record<string, string>; notice: string }
  | { type: "APPLY_STOCK"; productId: string; available: number; notice: string }
  | { type: "DISCARD" }
  | { type: "NEW_SALE" }
  | { type: "NOTICE"; notice: string | null };

const EDITABLE: ReadonlySet<SaleStatus> = new Set(["editando", "cobrada"]);

function mapLine(
  state: SaleState,
  productId: string,
  f: (l: CartLine) => CartLine,
): SaleState {
  return { ...state, lines: state.lines.map((l) => (l.product.id === productId ? f(l) : l)) };
}

/**
 * Después de tocar los pagos: sin pago en efectivo no hay "efectivo recibido"
 * que informar (el servidor lo rechaza con `CASH_RECEIVED_WITHOUT_CASH_TENDER`),
 * y al volver a un solo pago su importe deja de contar: sigue al total.
 */
function conPagos(state: SaleState, payments: PaymentRow[]): SaleState {
  const normalizados = payments.length === 1 ? [{ ...primerPago(payments), amount: "" }] : payments;
  const siguiente = { ...state, payments: normalizados };
  return hasCash(siguiente) ? siguiente : { ...siguiente, cashReceived: "" };
}

let pagoSeq = 0;
function nuevoPagoId(): string {
  pagoSeq += 1;
  return `p${pagoSeq}-${Date.now().toString(36)}`;
}

/** Una venta cobrada que recibe una edición arranca la siguiente, sin paso extra. */
function editable(state: SaleState): SaleState {
  return state.status === "cobrada" ? { ...initialSale } : state;
}

export function saleReducer(state: SaleState, action: SaleAction): SaleState {
  switch (action.type) {
    case "ADD_PRODUCT": {
      if (!EDITABLE.has(state.status)) return state;
      const base = editable(state);
      const existe = base.lines.some((l) => l.product.id === action.product.id);
      // Una unidad de VENTA: 1 unidad, o 1 kg (1000 g) de un producto por peso,
      // que el cajero corrige al peso real. Sumar 1 sería sumar un gramo.
      const paso = Math.max(1, action.product.base_units_per_sale_unit || 1);
      const lines = existe
        ? base.lines.map((l) =>
            l.product.id === action.product.id
              ? // El precio y el stock más recientes que se conocen.
                { ...l, product: action.product, quantity: l.quantity + paso }
              : l,
          )
        : [...base.lines, { product: action.product, quantity: paso, discountCents: 0 }];
      return { ...base, lines, notice: null };
    }
    case "SET_QUANTITY":
      if (state.status !== "editando" || action.quantity < 1) return state;
      return mapLine(state, action.productId, (l) => ({ ...l, quantity: action.quantity }));
    case "REMOVE":
      if (state.status !== "editando") return state;
      return { ...state, lines: state.lines.filter((l) => l.product.id !== action.productId) };
    case "SET_LINE_DISCOUNT":
      if (state.status !== "editando" || action.cents < 0) return state;
      return mapLine(state, action.productId, (l) => ({ ...l, discountCents: action.cents }));
    case "SET_LINE_PRICE":
      if (state.status !== "editando") return state;
      if (action.cents === null) {
        return mapLine(state, action.productId, (l) => ({
          ...l,
          manualPriceCents: null,
          manualPriceReason: undefined,
        }));
      }
      // Un precio manual sin motivo, o en cero, el servidor lo rechaza: mejor
      // no dejarlo entrar al carrito.
      if (action.cents <= 0 || action.reason.trim() === "") return state;
      return mapLine(state, action.productId, (l) => ({
        ...l,
        manualPriceCents: action.cents,
        manualPriceReason: action.reason.trim(),
      }));
    case "SET_GLOBAL_DISCOUNT":
      if (state.status !== "editando" || action.cents < 0) return state;
      return { ...state, globalDiscountCents: action.cents };
    case "SET_PAYMENT_METHOD":
      if (state.status !== "editando") return state;
      return conPagos(
        state,
        state.payments.map((p) => (p.id === action.id ? { ...p, method: action.method } : p)),
      );
    case "SET_PAYMENT_AMOUNT":
      if (state.status !== "editando") return state;
      return {
        ...state,
        payments: state.payments.map((p) =>
          p.id === action.id ? { ...p, amount: action.value } : p,
        ),
      };
    case "SPLIT_PAYMENT": {
      if (state.status !== "editando") return state;
      const usados = new Set(state.payments.map((p) => p.method));
      const libre = POS_PAYMENT_METHODS.find((m) => !usados.has(m));
      if (!libre) return state;
      const { totalCents } = previewTotals(state);
      // Al pasar de uno a dos, el primero se vuelve explícito con el total de
      // ahora; el nuevo sugiere lo que falte asignar (si falta algo).
      const actuales = isSplit(state)
        ? state.payments
        : [{ ...primerPago(state.payments), amount: centsToAmount(Math.max(totalCents, 0)) }];
      const resto = unassignedCents({ payments: actuales }, totalCents);
      return conPagos(state, [
        ...actuales,
        {
          id: nuevoPagoId(),
          method: libre,
          amount: resto !== null && resto > 0 ? centsToAmount(resto) : "",
        },
      ]);
    }
    case "REMOVE_PAYMENT":
      if (state.status !== "editando" || !isSplit(state)) return state;
      return conPagos(
        state,
        state.payments.filter((p) => p.id !== action.id),
      );
    case "SET_CASH_RECEIVED":
      if (state.status !== "editando") return state;
      return { ...state, cashReceived: action.value };
    case "SET_CUSTOMER":
      if (state.status !== "editando") return state;
      return { ...state, customerId: action.id, customerName: action.name };
    case "SEND":
      if (state.status !== "editando") return state;
      return { ...state, status: "enviando", payload: action.payload, notice: null };
    case "SUCCESS":
      return { ...state, status: "cobrada", result: action.result, payload: null, notice: null };
    case "REJECTED":
      // El servidor lo rechazó y revirtió todo: nada entró. Se puede corregir,
      // y el próximo cobro nace con id nuevo.
      return {
        ...state,
        status: "editando",
        payload: null,
        notice: action.notice,
        noticeTono: "error",
      };
    case "QUEUED":
      // La venta quedó guardada y se sube sola: el carrito se LIBERA y el
      // cajero sigue atendiendo. Trabar la caja hasta que vuelva la conexión
      // sería exactamente lo que B7 viene a evitar. Queda `cobrada` como una
      // confirmada: escanear arranca la siguiente, igual que después de cobrar.
      return {
        ...initialSale,
        status: "cobrada",
        guardada: action.recibo,
        notice: action.notice,
        noticeTono: "aviso",
      };
    case "APPLY_PRICES":
      if (state.status !== "editando") return state;
      return {
        ...state,
        notice: action.notice,
        noticeTono: "error",
        lines: state.lines.map((l) => {
          // Una línea cobrada a mano no se toca: el servidor devuelve como
          // "precio" el manual, y pisar con él el de lista dejaría la firma
          // del catálogo apuntando a otro número (PRICE_TOKEN_INVALID).
          if (l.manualPriceCents != null) return l;
          const precio = action.prices[l.product.id];
          if (!precio) return l;
          // Con firma, el precio de lista sólo puede ser el firmado: si el
          // servidor dice otro, la firma ya no sirve y se descarta — sin ella
          // el servidor cobra el de catálogo, que es justo el que informó.
          const firmaVigente = precio === l.product.sale_price_ars;
          return {
            ...l,
            product: {
              ...l.product,
              sale_price_ars: precio,
              price_token: firmaVigente ? l.product.price_token : null,
            },
          };
        }),
      };
    case "APPLY_STOCK":
      if (state.status !== "editando") return state;
      return {
        ...mapLine(state, action.productId, (l) => ({
          ...l,
          product: { ...l.product, stock_units: action.available },
        })),
        notice: action.notice,
        noticeTono: "error",
      };
    case "DISCARD":
    case "NEW_SALE":
      return { ...initialSale };
    case "NOTICE":
      return { ...state, notice: action.notice, noticeTono: "error" };
    default:
      return state;
  }
}

// ─── Clasificación de la respuesta del cobro ─────────────────────────────────

/**
 * ¿El cajero puede corregir esto en el mostrador y volver a cobrar?
 *
 * Es la ÚNICA pregunta que habilita borrar la venta local, y sólo en el primer
 * envío sincrónico: el carrito sigue bloqueado, nadie entregó nada y el cajero
 * está mirando la pantalla. Si la respuesta es no, la venta queda encolada.
 *
 * Se decide con `clasificarSync`, la misma y única clasificación que usa la
 * cola. Antes esto era un rango —"cualquier 4xx menos 408 y 429"— y por eso
 * borraba ante **402** (suscripción vencida) y **423** (mantenimiento), que son
 * justo los dos que se arreglan solos sin tocar la venta: el cajero ya había
 * cobrado y la venta desaparecía de los dos lados.
 */
export function esCorregibleEnElMostrador(info: PosErrorInfo): boolean {
  // Sólo lo que el SERVIDOR contestó. Una excepción local (`tipo: "local"`) va
  // a revisión en la cola, pero acá no prueba que la venta no entró: puede
  // saltar después de la respuesta, en un interceptor. Ante la duda, se encola.
  if (info.status === null) return false;
  if (clasificarSync(info) !== "revision") return false;
  // Estos dos 409 dicen lo contrario de "no entró": la clave YA tiene una venta.
  // Tratarlos como rechazo borraría una venta que existe en el servidor.
  if (info.code === "IDEMPOTENCY_KEY_REUSED" || info.code === "DUPLICATE_IDEMPOTENT") {
    return false;
  }
  return true;
}
