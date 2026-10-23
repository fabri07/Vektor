import { create } from "zustand";
import { persist } from "zustand/middleware";

/**
 * La impresora de ESTA PC (B12). Por PC: depende del papel y del driver.
 *
 * `pageSize` son las tres variantes que mide el spike B1 en el hardware real
 * (A: alto fijo largo, B: más angosto y corto, C: que decida el driver). El
 * default es A hasta que B1 diga cuál pagina bien en la térmica.
 */
export type PaperWidth = 80 | 58;
export type PageSize = "A" | "B" | "C";

interface PrintConfigState {
  paperWidth: PaperWidth;
  pageSize: PageSize;
  autoPrint: boolean;
  setPaperWidth: (w: PaperWidth) => void;
  setPageSize: (p: PageSize) => void;
  setAutoPrint: (v: boolean) => void;
}

export const usePosPrintConfigStore = create<PrintConfigState>()(
  persist(
    (set) => ({
      paperWidth: 80,
      pageSize: "A",
      autoPrint: true,
      setPaperWidth: (paperWidth) => set({ paperWidth }),
      setPageSize: (pageSize) => set({ pageSize }),
      setAutoPrint: (autoPrint) => set({ autoPrint }),
    }),
    { name: "vektor-pos-printer" },
  ),
);

/** El `@page` según la config. */
export function pageCss(paperWidth: PaperWidth, pageSize: PageSize): string {
  if (pageSize === "A") return `@page { size: ${paperWidth}mm 297mm; margin: 0; }`;
  if (pageSize === "B") return `@page { size: ${paperWidth - 8}mm 200mm; margin: 0; }`;
  return "@page { margin: 0; }";
}
