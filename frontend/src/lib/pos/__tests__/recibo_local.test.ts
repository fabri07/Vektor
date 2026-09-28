/**
 * El comprobante que sale cuando la caja cobró y no pudo avisarle al servidor.
 *
 * Lo que importa: que diga los MISMOS importes que va a tener el ticket
 * definitivo —si no, el cliente se lleva un papel que no coincide con la venta
 * registrada— y que se distinga del comprobante ya registrado.
 */
import { buildPayload, initialSale, saleReducer } from "@/lib/pos/cart";
import type { PosProduct, SaleState } from "@/lib/pos/cart";
import { reciboLocal } from "@/lib/pos/reciboLocal";
import { renderTicket } from "@/features/pos/ticketTemplate";

const alfajor: PosProduct = {
  id: "p1",
  name: "Alfajor",
  sale_price_ars: "100.00",
  sale_unit: "unit",
  base_units_per_sale_unit: 1,
  stock_units: 50,
  price_token: "tok",
};
const queso: PosProduct = {
  id: "p2",
  name: "Queso",
  sale_price_ars: "1234.56",
  sale_unit: "gram",
  base_units_per_sale_unit: 1000,
  stock_units: 5000,
  price_token: "tok2",
};

function venta(...acciones: Parameters<typeof saleReducer>[1][]): SaleState {
  return acciones.reduce(saleReducer, initialSale);
}

const datos = {
  businessName: "Kiosco El Rápido",
  cashierName: "Ana",
  terminalName: "Caja 1",
  customerName: null,
};

it("el descuento indivisible cierra: las líneas suman el total", () => {
  // $1 sobre tres unidades de $100: el mismo caso que el servidor resuelve con
  // el reparto por resto mayor. Si el comprobante provisional usara otra
  // fórmula, diría un total distinto del que después se registra.
  const s = venta(
    { type: "ADD_PRODUCT", product: alfajor },
    { type: "SET_QUANTITY", productId: "p1", quantity: 3 },
    { type: "SET_GLOBAL_DISCOUNT", cents: 100 },
  );
  const r = reciboLocal(buildPayload(s, "op-abc123456789", new Date()), s.lines, datos);
  expect(r.total_ars).toBe("299.00");
  const suma = r.lines.reduce((a, l) => a + Math.round(Number(l.line_total_ars) * 100), 0);
  expect(suma).toBe(29900);
});

it("una cantidad por peso se imprime con su unidad", () => {
  const s = venta(
    { type: "ADD_PRODUCT", product: queso },
    { type: "SET_QUANTITY", productId: "p2", quantity: 750 },
  );
  const r = reciboLocal(buildPayload(s, "op-abc123456789", new Date()), s.lines, datos);
  expect(r.lines[0]!.quantity_display).toBe("0,750 kg");
  // $1.234,56 el kilo × 750 g, redondeado una sola vez: lo mismo que el servidor.
  expect(r.lines[0]!.gross_ars).toBe("925.92");
});

it("el vuelto se calcula contra la parte en efectivo", () => {
  const s = venta(
    { type: "ADD_PRODUCT", product: alfajor },
    { type: "SET_CASH_RECEIVED", value: "500" },
  );
  const r = reciboLocal(buildPayload(s, "op-abc123456789", new Date()), s.lines, datos);
  expect(r.cash_received_ars).toBe("500.00");
  expect(r.cash_change_ars).toBe("400.00");
});

it("sale rotulado: no es el comprobante registrado", () => {
  const s = venta({ type: "ADD_PRODUCT", product: alfajor });
  const r = reciboLocal(buildPayload(s, "op-abc123456789", new Date()), s.lines, datos);
  expect(r.status).toBe("PENDIENTE");
  const doc = renderTicket(r, { paperWidth: 80, pageSize: "A" });
  expect(doc.bodyHtml).toContain("PENDIENTE DE SINCRONIZAR");
  expect(doc.bodyHtml).toContain("Kiosco El R");
});
