import { create } from "zustand";

/**
 * Estado de CUENTA que el servidor rechazó (panel SUPERADMIN, F1). No es el de la
 * suscripción: ése lo maneja `lib/subscription.ts` con sus propios códigos.
 *
 * Vive sólo en memoria: lo prende el interceptor de `api` cuando llega un 403
 * `TENANT_SUSPENDED` / `TENANT_READ_ONLY`, y se apaga cuando `/auth/me` vuelve a
 * contestar bien. Nunca toca la cola de ventas offline.
 */
export type AccountBlock = "SUSPENDED" | "READ_ONLY";

export const CODIGO_CUENTA_SUSPENDIDA = "TENANT_SUSPENDED";
export const CODIGO_CUENTA_SOLO_LECTURA = "TENANT_READ_ONLY";

export function bloqueoDeCodigo(codigo: string | undefined): AccountBlock | null {
  if (codigo === CODIGO_CUENTA_SUSPENDIDA) return "SUSPENDED";
  if (codigo === CODIGO_CUENTA_SOLO_LECTURA) return "READ_ONLY";
  return null;
}

interface AccountStatusStore {
  block: AccountBlock | null;
  message: string | null;
  setBlock: (block: AccountBlock, message?: string | null) => void;
  clear: () => void;
}

export const useAccountStatusStore = create<AccountStatusStore>((set) => ({
  block: null,
  message: null,
  setBlock: (block, message = null) => set({ block, message }),
  clear: () => set({ block: null, message: null }),
}));
