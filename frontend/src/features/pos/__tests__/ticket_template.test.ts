import { type PosReceipt, formatTicketDate, renderTicket } from "@/features/pos/ticketTemplate";

const recibo: PosReceipt = {
  id: "abc",
  number: "1A2B3C4D",
  business_name: "Kiosco Don Pedro",
  operation_date: "2026-09-25T23:50:07",
  status: "COMPLETED",
  lines: [
    {
      position: 0,
      product_name: "Yerba 1kg",
      quantity: 2,
      quantity_display: "2 u.",
      unit_price_list: "1000.00",
      gross_ars: "2000.00",
      discount_line_ars: "50.00",
      // Con su parte del descuento global ($100 repartido).
      line_total_ars: "1882.20",
    },
    {
      position: 1,
      product_name: "Queso",
      quantity: 750,
      quantity_display: "0,750 kg",
      unit_price_list: "1234.56",
      gross_ars: "925.92",
      discount_line_ars: "0.00",
      line_total_ars: "893.72",
    },
  ],
  // Dos pagos: el template ya soporta el mixto que habilita B9.
  tenders: [
    { payment_method: "cash", amount_ars: "2000.00" },
    { payment_method: "debit_card", amount_ars: "775.92" },
  ],
  subtotal_ars: "2875.92",
  discount_ars: "100.00",
  total_ars: "2775.92",
  cash_received_ars: "2500.00",
  cash_change_ars: "500.00",
  customer_name: null,
  cashier_name: "Ana",
  terminal_name: "Caja 1",
};

const config = { paperWidth: 80 as const, pageSize: "A" as const };

describe("ticket", () => {
  it("snapshot: descuento, vuelto y dos pagos", () => {
    expect(renderTicket(recibo, config).bodyHtml).toMatchSnapshot();
  });

  it("dice que no es fiscal y trae lo esencial", () => {
    const html = renderTicket(recibo, config).bodyHtml;
    expect(html).toContain("Comprobante no fiscal");
    expect(html).toContain("Ticket 1A2B3C4D");
    expect(html).toContain("25/09/2026 23:50");
    expect(html).toContain("Vuelto");
    expect(html).toContain("Tarjeta débito");
    expect(html).not.toContain("ANULADO");
  });

  it("las líneas suman el subtotal impreso: el global no se muestra por línea", () => {
    const html = renderTicket(recibo, config).bodyHtml;
    // `\s`: Intl pone un espacio duro entre el signo y el número.
    expect(html).toMatch(/1\.000,00<\/span><span>\$\s2\.000,00/); // bruto de la yerba
    expect(html).not.toContain("1.882,20"); // el total con la parte del global
    expect(html).toMatch(/Subtotal<\/span><span>\$\s2\.875,92/);
  });

  it("un ticket anulado lo dice", () => {
    expect(renderTicket({ ...recibo, status: "VOIDED" }, config).bodyHtml).toContain(
      "TICKET ANULADO",
    );
  });

  it("escapa todo dato: un nombre con HTML no se ejecuta en el iframe", () => {
    const html = renderTicket(
      {
        ...recibo,
        business_name: "<img src=x onerror=alert(1)>",
        lines: [{ ...recibo.lines[0]!, product_name: "<script>robar()</script>" }],
        customer_name: 'Ana "la" <b>',
        cashier_name: "&",
      },
      config,
    ).bodyHtml;
    expect(html).not.toContain("<script>");
    expect(html).not.toContain("<img");
    expect(html).not.toContain("<b>");
    expect(html).toContain("&lt;script&gt;");
  });

  it("la fecha es la de negocio: se corta el texto, no se convierte de zona", () => {
    expect(formatTicketDate("2026-09-25T23:50:07")).toBe("25/09/2026 23:50");
  });

  it("el @page sale de la config de la PC", () => {
    expect(renderTicket(recibo, config).css).toContain("size: 80mm 297mm");
    expect(renderTicket(recibo, { paperWidth: 58, pageSize: "C" }).css).toContain(
      "@page { margin: 0; }",
    );
  });
});
