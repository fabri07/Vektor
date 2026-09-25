import { formatCents, toCents } from "@/lib/pos/cart";
import { type PrintDocument, escapeHtml } from "@/lib/print/types";
import { PAYMENT_METHOD_LABELS } from "@/lib/suppliers";
import { type PageSize, type PaperWidth, pageCss } from "@/stores/posPrintConfigStore";

/**
 * El ticket de caja (B12), como HTML para imprimir.
 *
 * Función pura y no componente: el HTML va a un iframe del MISMO origen que la
 * app, así que un nombre de producto con `<script>` correría con acceso a la
 * sesión. Por eso TODO dato pasa por `escapeHtml`, en un solo lugar, y el test
 * lo verifica.
 */

export interface PosReceipt {
  id: string;
  number: string;
  business_name: string;
  operation_date: string;
  status: string;
  lines: {
    position: number;
    product_name: string;
    quantity: number;
    quantity_display: string;
    unit_price_list: string;
    gross_ars: string;
    /** Sólo el descuento propio de la línea; el global va en los totales. */
    discount_line_ars: string;
    line_total_ars: string;
  }[];
  tenders: { payment_method: string; amount_ars: string }[];
  subtotal_ars: string;
  discount_ars: string;
  total_ars: string;
  cash_received_ars: string | null;
  cash_change_ars: string | null;
  customer_name: string | null;
  cashier_name: string | null;
  terminal_name: string | null;
}

const e = escapeHtml;
const pesos = (monto: string) => e(formatCents(toCents(monto) ?? 0));

/**
 * "2026-09-25T23:50:07" → "25/09/2026 23:50". Se corta el texto, NO se pasa por
 * `Date`: es la hora de negocio ya guardada sin zona, y convertirla la movería.
 */
export function formatTicketDate(iso: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(iso);
  return m ? `${m[3]}/${m[2]}/${m[1]} ${m[4]}:${m[5]}` : iso;
}

function fila(izq: string, der: string, clase = ""): string {
  return `<div class="fila ${clase}"><span>${izq}</span><span>${der}</span></div>`;
}

export function ticketCss(paperWidth: PaperWidth, pageSize: PageSize): string {
  // Margen de impresión de la térmica: 80 mm de papel imprimen ~72 mm.
  const ancho = paperWidth === 80 ? 72 : 48;
  return `${pageCss(paperWidth, pageSize)}
html,body{margin:0;padding:0;background:#fff;color:#000}
body{width:${ancho}mm;font:12px/1.35 "Courier New",monospace;padding:2mm 0}
.centro{text-align:center}.negrita{font-weight:bold}
.grande{font-size:15px}.total{font-size:16px;font-weight:bold}
.fila{display:flex;justify-content:space-between;gap:4px}
.fila span:last-child{white-space:nowrap}
.sep{border-top:1px dashed #000;margin:4px 0}
.item{margin:2px 0}
.anulado{border:2px solid #000;text-align:center;font-weight:bold;margin:4px 0;padding:2px}`;
}

export function renderTicket(
  r: PosReceipt,
  config: { paperWidth: PaperWidth; pageSize: PageSize },
): PrintDocument {
  const partes: string[] = [];
  partes.push(`<div class="centro negrita grande">${e(r.business_name)}</div>`);
  partes.push(`<div class="centro">Comprobante no fiscal</div>`);
  partes.push('<div class="sep"></div>');
  partes.push(fila(`Ticket ${e(r.number)}`, e(formatTicketDate(r.operation_date))));
  if (r.terminal_name) partes.push(fila("Caja", e(r.terminal_name)));
  if (r.cashier_name) partes.push(fila("Atendió", e(r.cashier_name)));
  if (r.status === "VOIDED") partes.push('<div class="anulado">TICKET ANULADO</div>');
  partes.push('<div class="sep"></div>');

  for (const l of r.lines) {
    partes.push('<div class="item">');
    partes.push(`<div>${e(l.product_name)}</div>`);
    // Bruto y descuento PROPIO: así las líneas suman el subtotal impreso, y el
    // descuento global aparece una sola vez, abajo.
    partes.push(fila(`${e(l.quantity_display)} x ${pesos(l.unit_price_list)}`, pesos(l.gross_ars)));
    if ((toCents(l.discount_line_ars) ?? 0) > 0) {
      partes.push(fila("  Descuento", `-${pesos(l.discount_line_ars)}`));
    }
    partes.push("</div>");
  }

  partes.push('<div class="sep"></div>');
  if ((toCents(r.discount_ars) ?? 0) > 0) {
    partes.push(fila("Subtotal", pesos(r.subtotal_ars)));
    partes.push(fila("Descuento", `-${pesos(r.discount_ars)}`));
  }
  partes.push(fila("TOTAL", pesos(r.total_ars), "total"));
  for (const t of r.tenders) {
    const medio = PAYMENT_METHOD_LABELS[t.payment_method] ?? t.payment_method;
    partes.push(fila(e(medio === "Cuenta corriente" ? "Fiado" : medio), pesos(t.amount_ars)));
  }
  if (r.cash_received_ars !== null) partes.push(fila("Recibido", pesos(r.cash_received_ars)));
  if (r.cash_change_ars !== null && (toCents(r.cash_change_ars) ?? 0) > 0) {
    partes.push(fila("Vuelto", pesos(r.cash_change_ars), "negrita"));
  }
  if (r.customer_name) partes.push(fila("Cliente", e(r.customer_name)));
  partes.push('<div class="sep"></div>');
  partes.push('<div class="centro">Gracias por su compra</div>');
  partes.push('<div class="centro">Comprobante no fiscal</div>');

  return {
    title: `Ticket ${e(r.number)}`,
    bodyHtml: partes.join(""),
    css: ticketCss(config.paperWidth, config.pageSize),
  };
}

/** Un ticket de muestra, para probar la impresora desde la caja. */
export const TICKET_DE_PRUEBA: PosReceipt = {
  id: "prueba",
  number: "PRUEBA",
  business_name: "Prueba de impresión",
  operation_date: "2026-01-01T12:00:00",
  status: "COMPLETED",
  lines: [
    {
      position: 1,
      product_name: "Producto de prueba con un nombre largo para ver el corte",
      quantity: 2,
      quantity_display: "2 u.",
      unit_price_list: "1234.56",
      gross_ars: "2469.12",
      discount_line_ars: "0.00",
      line_total_ars: "2469.12",
    },
  ],
  tenders: [{ payment_method: "cash", amount_ars: "2469.12" }],
  subtotal_ars: "2469.12",
  discount_ars: "0.00",
  total_ars: "2469.12",
  cash_received_ars: "3000.00",
  cash_change_ars: "530.88",
  customer_name: null,
  cashier_name: null,
  terminal_name: null,
};
