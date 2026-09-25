import { renderLabels } from "@/features/products/labelSheet";

const con = { id: "1", name: "Florero <azul>", internal_sku: "VKT-0123456789AB", sale_price_ars: "1500.00" };
const sin = { id: "2", name: "Balde", internal_sku: null, sale_price_ars: "900.00" };
const opts = { format: "a4" as const, showPrice: true, paperWidth: 80 as const, pageSize: "A" as const };

describe("etiquetas", () => {
  it("una etiqueta por copia; los que no tienen código interno se avisan y no se imprimen", () => {
    const r = renderLabels(
      [
        { product: con, copies: 3 },
        { product: sin, copies: 5 },
      ],
      opts,
    );
    expect(r.count).toBe(3);
    expect(r.skipped).toEqual(["Balde"]);
    expect(r.doc.bodyHtml.match(/class="etiqueta"/g)).toHaveLength(3);
    expect(r.doc.bodyHtml).toContain("<svg");
    expect(r.doc.bodyHtml).toContain("VKT-0123456789AB");
  });

  it("escapa el nombre", () => {
    const html = renderLabels([{ product: con, copies: 1 }], opts).doc.bodyHtml;
    expect(html).toContain("Florero &lt;azul&gt;");
  });

  it("sin precio si se pide; A4 va en grilla y el rollo no", () => {
    const a4 = renderLabels([{ product: con, copies: 1 }], { ...opts, showPrice: false });
    expect(a4.doc.bodyHtml).not.toContain("class=\"precio\"");
    expect(a4.doc.bodyHtml.startsWith('<div class="hoja">')).toBe(true);
    expect(a4.doc.css).toContain("size:A4");
    const rollo = renderLabels([{ product: con, copies: 1 }], { ...opts, format: "rollo" });
    expect(rollo.doc.bodyHtml.startsWith('<div class="etiqueta">')).toBe(true);
    expect(rollo.doc.css).toContain("size: 80mm 297mm");
  });
});
