import { create } from "zustand";
import { persist } from "zustand/middleware";

/**
 * Esta PC como caja habilitada (B6).
 *
 * Se persiste con su PROPIA clave de localStorage y `authStore.logout()` no la
 * toca: la PC sigue siendo caja aunque cambie quién está logueado. El secreto
 * identifica la PC, no a la persona — por sí solo no da acceso a nada, siempre
 * viaja junto con el login del usuario.
 */

export interface PosTerminalLocal {
  terminalId: string;
  name: string;
  secret: string;
  tenantId: string;
}

interface PosTerminalState {
  terminal: PosTerminalLocal | null;
  /** El servidor la rechazó (dada de baja o desconocida). */
  invalid: boolean;
  setTerminal: (t: PosTerminalLocal) => void;
  markInvalid: () => void;
  clear: () => void;
}

export const usePosTerminalStore = create<PosTerminalState>()(
  persist(
    (set) => ({
      terminal: null,
      invalid: false,
      setTerminal: (terminal) => set({ terminal, invalid: false }),
      markInvalid: () => set({ invalid: true }),
      clear: () => set({ terminal: null, invalid: false }),
    }),
    { name: "vektor-pos-terminal" },
  ),
);

/** Rutas en las que viaja el header: las que escribe una caja. */
export function llevaHeaderDeTerminal(url: string | undefined): boolean {
  if (!url) return false;
  return url.startsWith("/pos/") || /^\/products\/[^/]+\/barcode$/.test(url);
}
