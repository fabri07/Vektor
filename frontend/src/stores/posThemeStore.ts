import { create } from "zustand";
import { persist } from "zustand/middleware";

/**
 * Tema de la caja, por PC (B13): una caja al sol y otra en un rincón oscuro no
 * piden lo mismo. Sólo cambia la pantalla — el ticket impreso lleva su propio
 * CSS y sale siempre negro sobre blanco.
 */
export type PosTheme = "claro" | "oscuro";

interface PosThemeState {
  theme: PosTheme;
  toggle: () => void;
}

export const usePosThemeStore = create<PosThemeState>()(
  persist(
    (set, get) => ({
      theme: "claro",
      toggle: () => set({ theme: get().theme === "claro" ? "oscuro" : "claro" }),
    }),
    { name: "vektor-pos-tema" },
  ),
);

/** La clase que define las variables `--pos-*` (globals.css). */
export function posThemeClass(theme: PosTheme): string {
  return theme === "oscuro" ? "pos-oscuro" : "pos-claro";
}
