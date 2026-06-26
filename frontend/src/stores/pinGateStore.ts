import { create } from "zustand";

export type PinMode = "verify" | "setup";

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

  succeed: () => {
    get()._resolve?.();
    set({ isOpen: false, _resolve: null, _reject: null, _promise: null });
  },

  cancel: () => {
    get()._reject?.(new Error("PIN_CANCELLED"));
    set({ isOpen: false, _resolve: null, _reject: null, _promise: null });
  },
}));
