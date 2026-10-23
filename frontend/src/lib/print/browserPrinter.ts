import type { PrintDocument, ReceiptPrinter } from "@/lib/print/types";

/**
 * Imprime en un iframe oculto: los estilos de la app no se mezclan con los del
 * ticket, y el `@page` del ticket no afecta a la pantalla.
 *
 * El iframe se quita SIEMPRE: si `print()` tira, en el acto; si no, al
 * `afterprint` (o a los 60 s, por si el navegador no lo emite). Un iframe que
 * queda colgado por cada ticket es una pérdida de memoria en una caja que no se
 * reinicia en todo el día.
 */
export interface BrowserPrinterOptions {
  /** Inyectable en los tests: jsdom no implementa `print()`. */
  printWindow?: (w: Window) => void;
  fallbackRemoveMs?: number;
}

export function createBrowserPrinter(options: BrowserPrinterOptions = {}): ReceiptPrinter {
  const printWindow = options.printWindow ?? ((w: Window) => w.print());
  const fallbackMs = options.fallbackRemoveMs ?? 60_000;

  return {
    async print(doc: PrintDocument): Promise<void> {
      const iframe = document.createElement("iframe");
      iframe.setAttribute("aria-hidden", "true");
      iframe.style.cssText =
        "position:fixed;right:0;bottom:0;width:0;height:0;border:0;visibility:hidden";
      document.body.appendChild(iframe);

      let quitado = false;
      const quitar = () => {
        if (quitado) return;
        quitado = true;
        iframe.remove();
      };

      try {
        const w = iframe.contentWindow;
        const d = iframe.contentDocument;
        if (!w || !d) throw new Error("No se pudo preparar la impresión.");
        d.open();
        d.write(
          `<!doctype html><html><head><meta charset="utf-8"><title>${doc.title}</title>` +
            `<style>${doc.css}</style></head><body>${doc.bodyHtml}</body></html>`,
        );
        d.close();
        // Un tick para que el iframe termine de maquetar antes de imprimir.
        await new Promise((r) => setTimeout(r, 30));
        w.addEventListener("afterprint", quitar, { once: true });
        const timer = setTimeout(quitar, fallbackMs);
        w.addEventListener("afterprint", () => clearTimeout(timer), { once: true });
        w.focus();
        printWindow(w);
      } catch (e) {
        quitar();
        throw e;
      }
    },
  };
}
