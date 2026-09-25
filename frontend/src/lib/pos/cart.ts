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
}

export interface CartLine {
  product: PosProduct;
  /** En UNIDADES BASE (gramos, mililitros o unidades), como el servidor. */
  quantity: number;
  discountCents: number;
}

export interface PosOperationPayload {
  client_operation_id: string;
  customer_id: string | null;
  operation_date: string;
  discount_ars: string;
  cash_received_ars: string | null;
  items: { product_id: string; quantity: number; discount_ars: string }[];
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
 * - `en_duda`: el cobro no tuvo respuesta (red, timeout, 5xx). Puede haber
 *   entrado. El carrito queda BLOQUEADO con el payload congelado: editarlo y
 *   cobrar con un id nuevo cobraría dos veces si la primera había entrado.
 * - `cobrada`: el servidor confirmó.
 */
export type SaleStatus = "editando" | "enviando" | "en_duda" | "cobrada";

export interface SaleState {
  status: SaleStatus;
  lines: CartLine[];
  globalDiscountCents: number;
  paymentMethod: string;
  /** Lo que escribió el cajero; vacío = no informado. */
  cashReceived: string;
  customerId: string | null;
  customerName: string | null;
  /** El cuerpo EXACTO que se mandó. El reintento reenvía este objeto, no uno nuevo. */
  payload: PosOperationPayload | null;
  result: PosOperationResult | null;
  /** Último rechazo o aviso, para mostrar. */
  notice: string | null;
}

export const initialSale: SaleState = {
  status: "editando",
  lines: [],
  globalDiscountCents: 0,
  paymentMethod: "cash",
  cashReceived: "",
  customerId: null,
  customerName: null,
  payload: null,
  result: null,
  notice: null,
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

export function lineNetCents(line: CartLine): number {
  const precio = toCents(line.product.sale_price_ars) ?? 0;
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

/** Lo que impide cobrar, o `null` si se puede. Evita mandar lo que el servidor rechazaría seguro. */
export function cannotCharge(state: SaleState): string | null {
  if (state.lines.length === 0) return "El carrito está vacío.";
  for (const l of state.lines) {
    if (lineNetCents(l) < 0) return `El descuento de «${l.product.name}» supera su importe.`;
  }
  const { totalCents } = previewTotals(state);
  if (totalCents < 0) return "El descuento supera el total de la venta.";
  if (totalCents === 0) return "Una venta en $0 no se puede cobrar.";
  if (state.paymentMethod === "account" && !state.customerId) {
    return "Para vender fiado hay que elegir el cliente.";
  }
  if (state.paymentMethod === "cash" && state.cashReceived.trim() !== "") {
    const recibido = toCents(state.cashReceived);
    if (recibido === null) return "El efectivo recibido no es un monto válido.";
    if (recibido < totalCents) return "El efectivo recibido no alcanza para el total.";
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
export function buildPayload(state: SaleState, id: string, now: Date): PosOperationPayload {
  const { totalCents } = previewTotals(state);
  const efectivo = state.paymentMethod === "cash" && state.cashReceived.trim() !== "";
  return {
    client_operation_id: id,
    customer_id: state.paymentMethod === "account" ? state.customerId : null,
    operation_date: localNaiveIso(now),
    discount_ars: centsToAmount(state.globalDiscountCents),
    cash_received_ars: efectivo ? centsToAmount(toCents(state.cashReceived) ?? 0) : null,
    items: state.lines.map((l) => ({
      product_id: l.product.id,
      quantity: l.quantity,
      discount_ars: centsToAmount(l.discountCents),
    })),
    tenders: [{ payment_method: state.paymentMethod, amount_ars: centsToAmount(totalCents) }],
  };
}

// ─── Reducer ─────────────────────────────────────────────────────────────────

export type SaleAction =
  | { type: "ADD_PRODUCT"; product: PosProduct }
  | { type: "SET_QUANTITY"; productId: string; quantity: number }
  | { type: "REMOVE"; productId: string }
  | { type: "SET_LINE_DISCOUNT"; productId: string; cents: number }
  | { type: "SET_GLOBAL_DISCOUNT"; cents: number }
  | { type: "SET_PAYMENT"; method: string }
  | { type: "SET_CASH_RECEIVED"; value: string }
  | { type: "SET_CUSTOMER"; id: string | null; name: string | null }
  | { type: "SEND"; payload: PosOperationPayload }
  | { type: "SUCCESS"; result: PosOperationResult }
  | { type: "REJECTED"; notice: string }
  | { type: "UNKNOWN"; notice: string }
  | { type: "RESTORE_PENDING"; payload: PosOperationPayload; lines: CartLine[] }
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
      const lines = existe
        ? base.lines.map((l) =>
            l.product.id === action.product.id
              ? // El precio y el stock más recientes que se conocen.
                { ...l, product: action.product, quantity: l.quantity + 1 }
              : l,
          )
        : [...base.lines, { product: action.product, quantity: 1, discountCents: 0 }];
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
    case "SET_GLOBAL_DISCOUNT":
      if (state.status !== "editando" || action.cents < 0) return state;
      return { ...state, globalDiscountCents: action.cents };
    case "SET_PAYMENT":
      if (state.status !== "editando") return state;
      return {
        ...state,
        paymentMethod: action.method,
        cashReceived: action.method === "cash" ? state.cashReceived : "",
      };
    case "SET_CASH_RECEIVED":
      if (state.status !== "editando") return state;
      return { ...state, cashReceived: action.value };
    case "SET_CUSTOMER":
      if (state.status !== "editando") return state;
      return { ...state, customerId: action.id, customerName: action.name };
    case "SEND":
      if (state.status !== "editando" && state.status !== "en_duda") return state;
      return { ...state, status: "enviando", payload: action.payload, notice: null };
    case "SUCCESS":
      return { ...state, status: "cobrada", result: action.result, payload: null, notice: null };
    case "REJECTED":
      // El servidor lo rechazó y revirtió todo: nada entró. Se puede corregir,
      // y el próximo cobro nace con id nuevo.
      return { ...state, status: "editando", payload: null, notice: action.notice };
    case "UNKNOWN":
      return { ...state, status: "en_duda", notice: action.notice };
    case "RESTORE_PENDING":
      return {
        ...initialSale,
        status: "en_duda",
        lines: action.lines,
        payload: action.payload,
        notice: "Hay un cobro que quedó sin confirmar. Reintentalo antes de seguir.",
      };
    case "APPLY_PRICES":
      if (state.status !== "editando") return state;
      return {
        ...state,
        notice: action.notice,
        lines: state.lines.map((l) => {
          const precio = action.prices[l.product.id];
          return precio ? { ...l, product: { ...l.product, sale_price_ars: precio } } : l;
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
      };
    case "DISCARD":
    case "NEW_SALE":
      return { ...initialSale };
    case "NOTICE":
      return { ...state, notice: action.notice };
    default:
      return state;
  }
}

// ─── Clasificación de la respuesta del cobro ─────────────────────────────────

export interface HttpErrorLike {
  /** `null` = no hubo respuesta. */
  status: number | null;
}

/**
 * ¿El cobro quedó definido o en duda?
 *
 * Sólo un 4xx definitivo prueba que no entró nada (el servidor revierte la
 * transacción entera). Sin respuesta (red, timeout) o con un 5xx / 408 / 429,
 * el cobro PUDO haber entrado: se reintenta con el mismo payload, nunca con
 * uno nuevo.
 */
export function isDefinitiveRejection(err: HttpErrorLike, code: string | null): boolean {
  if (err.status === null) return false;
  // Estos dos 409 dicen lo contrario de "no entró": la clave YA tiene una venta.
  // Desbloquear el carrito acá es exactamente el doble cobro que el estado
  // `en_duda` existe para impedir.
  if (code === "IDEMPOTENCY_KEY_REUSED" || code === "DUPLICATE_IDEMPOTENT") return false;
  return err.status >= 400 && err.status < 500 && err.status !== 408 && err.status !== 429;
}
