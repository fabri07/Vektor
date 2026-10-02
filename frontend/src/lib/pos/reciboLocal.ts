/**
 * El ticket que sale cuando la caja cobró y todavía no pudo avisarle al servidor.
 *
 * Con red, el recibo lo arma el servidor (`GET /pos/operations/{id}/receipt`) y
 * es la fuente de verdad. Sin red no hay a quién preguntarle, así que se arma
 * con lo que la caja ya tiene congelado: el payload del cobro, el catálogo
 * local y el nombre del negocio que vino con él.
 *
 * Sale rotulado **provisional**: no tiene número de ticket del servidor —ese
 * número es `id.hex[:8]` y el id lo genera la base— y la venta todavía no está
 * registrada. Cuando sincroniza, el ticket definitivo se puede reimprimir desde
 * el servidor con su número real, y va a tener uno distinto del provisional.
 */

import {
  centsToAmount,
  lineGrossCents,
  lineNetCents,
  precioEfectivoCents,
  toCents,
} from "@/lib/pos/cart";
import type { CartLine, PosOperationPayload } from "@/lib/pos/cart";
import { formatearCantidad } from "@/lib/pos/saleUnit";
import type { PosReceipt } from "@/features/pos/ticketTemplate";

export interface DatosDelRecibo {
  businessName: string;
  cashierName: string | null;
  terminalName: string | null;
  customerName: string | null;
}

/** Los 8 primeros del id del cliente, para poder nombrar el ticket provisional. */
export function numeroProvisional(clientOperationId: string): string {
  return clientOperationId.replace(/[^A-Za-z0-9]/g, "").slice(0, 8).toUpperCase();
}

/**
 * Arma el recibo local a partir de lo que se cobró.
 *
 * El descuento global se reparte con la MISMA fórmula que el servidor
 * (`lib/pos/cart`), así que el ticket provisional y el definitivo dicen los
 * mismos importes — lo único que cambia es el número y la leyenda.
 */
export function reciboLocal(
  payload: PosOperationPayload,
  lines: CartLine[],
  datos: DatosDelRecibo,
): PosReceipt {
  const brutos = lines.map((l) =>
    lineGrossCents(
      // El que se cobró: el manual si lo hubo.
      precioEfectivoCents(l),
      l.quantity,
      l.product.base_units_per_sale_unit,
    ),
  );
  const netos = lines.map((_l, i) => (netoDeLinea(lines, i) ?? 0));
  const subtotal = netos.reduce((a, b) => a + b, 0);
  const globalCents = toCents(payload.discount_ars) ?? 0;

  // Mismo reparto que `domain/pos_operation.py`: parte entera por importe y el
  // residuo de a un centavo por resto mayor. Sin esto, la suma de las líneas
  // impresas no daría el total impreso.
  const partes = netos.map((n) => (subtotal ? Math.floor((globalCents * n) / subtotal) : 0));
  const restos = netos.map((n) => (subtotal ? (globalCents * n) % subtotal : 0));
  let residuo = globalCents - partes.reduce((a, b) => a + b, 0);
  const orden = netos
    .map((n, i) => ({ i, resto: restos[i] ?? 0, neto: n }))
    .sort((a, b) => b.resto - a.resto || b.neto - a.neto || a.i - b.i);
  for (const { i } of orden) {
    if (residuo <= 0) break;
    partes[i] = (partes[i] ?? 0) + 1;
    residuo -= 1;
  }

  const renglones = lines.map((l, i) => {
    const bruto = brutos[i] ?? 0;
    const descuentoLinea = l.discountCents;
    const total = (netos[i] ?? 0) - (partes[i] ?? 0);
    return {
      position: i,
      product_name: l.product.name,
      quantity: l.quantity,
      quantity_display: formatearCantidad(
        l.quantity,
        l.product.sale_unit,
        l.product.base_units_per_sale_unit,
      ),
      unit_price_list: centsToAmount(precioEfectivoCents(l)),
      gross_ars: centsToAmount(bruto),
      discount_line_ars: centsToAmount(descuentoLinea),
      line_total_ars: centsToAmount(total),
    };
  });

  const efectivo = payload.tenders
    .filter((t) => t.payment_method === "cash")
    .reduce((a, t) => a + (toCents(t.amount_ars) ?? 0), 0);
  const recibido = payload.cash_received_ars === null ? null : toCents(payload.cash_received_ars);

  return {
    id: payload.client_operation_id,
    number: numeroProvisional(payload.client_operation_id),
    business_name: datos.businessName,
    operation_date: payload.operation_date,
    // No es un estado del servidor: es la leyenda que distingue este papel del
    // comprobante ya registrado.
    status: "PENDIENTE",
    lines: renglones,
    tenders: payload.tenders.map((t) => ({
      payment_method: t.payment_method,
      amount_ars: t.amount_ars,
    })),
    subtotal_ars: centsToAmount(subtotal),
    discount_ars: centsToAmount(globalCents),
    total_ars: centsToAmount(subtotal - globalCents),
    cash_received_ars: recibido === null ? null : centsToAmount(recibido),
    cash_change_ars: recibido === null ? null : centsToAmount(recibido - efectivo),
    customer_name: datos.customerName,
    cashier_name: datos.cashierName,
    terminal_name: datos.terminalName,
  };
}

function netoDeLinea(lines: CartLine[], i: number): number | null {
  const linea = lines[i];
  return linea === undefined ? null : lineNetCents(linea);
}
