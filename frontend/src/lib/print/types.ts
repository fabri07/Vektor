/**
 * Impresión de la caja (B12).
 *
 * La interfaz existe para que un día una impresión directa a la térmica
 * (ESC/POS) reemplace al navegador sin tocar la caja. Hoy hay una sola
 * implementación, `browserPrinter`: `window.print()` en un iframe, que con
 * Chrome `--kiosk-printing` sale sin diálogo.
 */
export interface PrintDocument {
  title: string;
  /** HTML del cuerpo, YA ESCAPADO. */
  bodyHtml: string;
  css: string;
}

export interface ReceiptPrinter {
  /**
   * Resuelve cuando el documento se mandó a imprimir. El navegador NO confirma
   * que salió papel: por eso la venta se confirma antes y siempre se puede
   * reimprimir.
   */
  print(doc: PrintDocument): Promise<void>;
}

/** Escapa texto para meterlo en HTML. Todo lo que viene de datos pasa por acá. */
export function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (c) =>
    c === "&" ? "&amp;" : c === "<" ? "&lt;" : c === ">" ? "&gt;" : c === '"' ? "&quot;" : "&#39;",
  );
}
