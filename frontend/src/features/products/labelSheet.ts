import { code128Svg } from "@/lib/barcode/code128";
import { formatCents, toCents } from "@/lib/pos/cart";
import { type PrintDocument, escapeHtml } from "@/lib/print/types";
import { type PageSize, type PaperWidth, pageCss } from "@/stores/posPrintConfigStore";

/**
 * Etiquetas con el código interno del producto (B12), para lo que no trae
 * código de fábrica (decoración, limpieza): el `internal_sku` (`VKT-…`) en
 * Code 128, que la caja ya reconoce como `INTERNAL_SKU` (B4).
 *
 * Dos formatos:
 * - `a4`: hoja de etiquetas adhesivas 3 × 8 (70 × 37 mm), las más comunes.
 * - `rollo`: una por fila en la térmica de la caja — muchos negocios no van a
 *   tener otra impresora.
 * Las medidas son las nominales: una marca de hojas puede pedir calibrar.
 */

export type LabelFormat = "a4" | "rollo";

export interface LabelProduct {
  id: string;
  name: string;
  internal_sku?: string | null;
  sale_price_ars: string;
}

export interface LabelRequest {
  product: LabelProduct;
  copies: number;
}

export function labelsCss(format: LabelFormat, paperWidth: PaperWidth, pageSize: PageSize): string {
  if (format === "a4") {
    return `@page{size:A4;margin:0}
html,body{margin:0;padding:0}
.hoja{display:grid;grid-template-columns:repeat(3,70mm);grid-auto-rows:37mm}
.etiqueta{box-sizing:border-box;padding:2mm 3mm;overflow:hidden;font:10px/1.2 Arial,sans-serif;
display:flex;flex-direction:column;justify-content:space-between;break-inside:avoid}
.nombre{font-weight:bold;max-height:2.4em;overflow:hidden}
.precio{font-size:13px;font-weight:bold}
.codigo svg{width:64mm;height:11mm}
.sku{font:9px monospace;text-align:center}`;
  }
  const ancho = paperWidth === 80 ? 72 : 48;
  // En el rollo, el mismo `@page` que el ticket de esta PC (lo mide B1).
  return `${pageCss(paperWidth, pageSize)}
html,body{margin:0;padding:0}
.etiqueta{box-sizing:border-box;width:${ancho}mm;padding:2mm 0 3mm;font:11px/1.2 Arial,sans-serif;
border-bottom:1px dashed #000;break-inside:avoid}
.nombre{font-weight:bold}
.precio{font-size:14px;font-weight:bold}
.codigo svg{width:${ancho}mm;height:12mm}
.sku{font:10px monospace;text-align:center}`;
}

export interface LabelsResult {
  doc: PrintDocument;
  /** Los que no tienen código interno: se avisan, no se imprimen en blanco. */
  skipped: string[];
  count: number;
}

export function renderLabels(
  requests: LabelRequest[],
  opts: { format: LabelFormat; showPrice: boolean; paperWidth: PaperWidth; pageSize: PageSize },
): LabelsResult {
  const skipped: string[] = [];
  const etiquetas: string[] = [];
  for (const { product, copies } of requests) {
    const sku = product.internal_sku?.trim();
    if (!sku) {
      skipped.push(product.name);
      continue;
    }
    const una =
      `<div class="etiqueta"><div class="nombre">${escapeHtml(product.name)}</div>` +
      (opts.showPrice
        ? `<div class="precio">${escapeHtml(formatCents(toCents(product.sale_price_ars) ?? 0))}</div>`
        : "") +
      `<div class="codigo">${code128Svg(sku)}</div>` +
      `<div class="sku">${escapeHtml(sku)}</div></div>`;
    for (let i = 0; i < Math.max(0, Math.floor(copies)); i++) etiquetas.push(una);
  }
  const cuerpo = etiquetas.join("");
  return {
    doc: {
      title: "Etiquetas",
      bodyHtml: opts.format === "a4" ? `<div class="hoja">${cuerpo}</div>` : cuerpo,
      css: labelsCss(opts.format, opts.paperWidth, opts.pageSize),
    },
    skipped,
    count: etiquetas.length,
  };
}
