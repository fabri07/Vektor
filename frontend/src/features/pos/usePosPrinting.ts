"use client";

import { useCallback, useState } from "react";

import { TICKET_DE_PRUEBA, renderTicket } from "@/features/pos/ticketTemplate";
import { createBrowserPrinter } from "@/lib/print/browserPrinter";
import { posService } from "@/services/pos.service";
import { usePosPrintConfigStore } from "@/stores/posPrintConfigStore";

const printer = createBrowserPrinter();

/**
 * Imprimir desde la caja (B12). Siempre DESPUÉS de que el servidor confirmó la
 * venta, y leyendo el recibo del servidor —el mismo que usa la reimpresión—,
 * nunca armándolo con lo que la caja tiene en memoria.
 *
 * Un fallo de impresión no toca el cobro: queda un aviso con "Reimprimir".
 */
export function usePosPrinting() {
  const [printError, setPrintError] = useState<string | null>(null);

  const printReceipt = useCallback(async (operationId: string): Promise<boolean> => {
    setPrintError(null);
    const { paperWidth, pageSize } = usePosPrintConfigStore.getState();
    try {
      const recibo = await posService.getReceipt(operationId);
      await printer.print(renderTicket(recibo, { paperWidth, pageSize }));
      return true;
    } catch {
      setPrintError("No se pudo imprimir el ticket. La venta está cobrada: podés reimprimirlo.");
      return false;
    }
  }, []);

  const printTest = useCallback(async (): Promise<void> => {
    setPrintError(null);
    const { paperWidth, pageSize } = usePosPrintConfigStore.getState();
    try {
      await printer.print(renderTicket(TICKET_DE_PRUEBA, { paperWidth, pageSize }));
    } catch {
      setPrintError("No se pudo imprimir la prueba. Revisá la impresora.");
    }
  }, []);

  return { printReceipt, printTest, printError, clearPrintError: () => setPrintError(null) };
}
