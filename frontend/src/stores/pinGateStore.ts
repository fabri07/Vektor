import { create } from "zustand";

export type PinMode = "verify" | "setup" | "reset";

/**
 * El usuario cerró el modal del PIN. No es un fallo: la acción simplemente no
 * se hace. Hasta 2026-10 el interceptor devolvía en su lugar el 428 original, y
 * cada pantalla lo mostraba como un error inventado ("¿nombre repetido?").
 */
export class PinCancelledError extends Error {
  constructor() {
    super("PIN_CANCELLED");
    this.name = "PinCancelledError";
  }
}

export function esPinCancelado(e: unknown): boolean {
  return e instanceof PinCancelledError;
}

interface PinGateState {
  isOpen: boolean;
  mode: PinMode;
  _resolve: (() => void) | null;
  _reject: ((reason?: unknown) => void) | null;
  _promise: Promise<void> | null;
  /**
   * Pide el PIN y resuelve cuando se verifica (abre la ventana de step-up).
   * Single-flight: si ya hay un modal abierto, devuelve la misma promesa.
   */
  requirePin: (mode?: PinMode) => Promise<void>;
  /** Abre el modal en modo setup sin promesa pendiente (primera sesión). */
  openSetup: () => void;
  /** Abre el modal para restablecer el PIN con la contraseña (PIN olvidado). */
  openReset: () => void;
  /** Llamado por el modal cuando el PIN se verificó/configuró OK. */
  succeed: () => void;
  /** Llamado por el modal al cancelar: rechaza la acción original. */
  cancel: () => void;
}

export const usePinGateStore = create<PinGateState>((set, get) => ({
  isOpen: false,
  mode: "verify",
  _resolve: null,
  _reject: null,
  _promise: null,

  requirePin: (mode = "verify") => {
    const state = get();
    if (state._promise) return state._promise;
    let resolveFn!: () => void;
    let rejectFn!: (reason?: unknown) => void;
    const p = new Promise<void>((res, rej) => {
      resolveFn = res;
      rejectFn = rej;
    });
    // Si ya hay un modal abierto (p.ej. el setup de primera sesión, que se abre
    // sin promesa), nos enganchamos a ÉL sin cambiarle el modo: cuando el usuario
    // termina ese flujo, succeed() resuelve esta promesa. Así un 428 no pisa un
    // setup en curso ni borra lo tipeado.
    if (state.isOpen) {
      set({ _resolve: resolveFn, _reject: rejectFn, _promise: p });
    } else {
      set({ isOpen: true, mode, _resolve: resolveFn, _reject: rejectFn, _promise: p });
    }
    return p;
  },

  openSetup: () => {
    if (get().isOpen) return;
    set({ isOpen: true, mode: "setup" });
  },

  openReset: () => {
    if (get().isOpen) return;
    set({ isOpen: true, mode: "reset" });
  },

  succeed: () => {
    get()._resolve?.();
    set({ isOpen: false, _resolve: null, _reject: null, _promise: null });
  },

  cancel: () => {
    get()._reject?.(new PinCancelledError());
    set({ isOpen: false, _resolve: null, _reject: null, _promise: null });
  },
}));
