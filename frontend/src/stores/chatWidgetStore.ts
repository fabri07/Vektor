"use client";

import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { ChatUiContext } from "@/services/agent.service";

/** Mensaje en cola para autoenviar al abrir el widget (ej. desde el banner de
 * alerta del dashboard) — se consume una sola vez y se limpia. */
export interface PendingPrompt {
  message: string;
  uiContext?: ChatUiContext;
}

interface ChatWidgetState {
  isOpen: boolean;
  /** Posición del ícono flotante (px desde la esquina sup-izq). null = default abajo-derecha. */
  posX: number | null;
  posY: number | null;
  pendingPrompt: PendingPrompt | null;
  open: () => void;
  close: () => void;
  toggle: () => void;
  setPosition: (x: number, y: number) => void;
  /** Abre el widget con un mensaje + ui_context ya armados (consumir con
   * `consumePendingPrompt`). */
  openWithPrompt: (prompt: PendingPrompt) => void;
  consumePendingPrompt: () => PendingPrompt | null;
}

export const useChatWidgetStore = create<ChatWidgetState>()(
  persist(
    (set, get) => ({
      isOpen: false,
      posX: null,
      posY: null,
      pendingPrompt: null,
      open: () => set({ isOpen: true }),
      close: () => set({ isOpen: false }),
      toggle: () => set((s) => ({ isOpen: !s.isOpen })),
      setPosition: (x, y) => set({ posX: x, posY: y }),
      openWithPrompt: (prompt) => set({ isOpen: true, pendingPrompt: prompt }),
      consumePendingPrompt: () => {
        const prompt = get().pendingPrompt;
        if (prompt) set({ pendingPrompt: null });
        return prompt;
      },
    }),
    {
      name: "vektor_chat_widget",
      // No persistimos isOpen/pendingPrompt — el widget arranca cerrado y sin
      // cola en cada sesión.
      partialize: (state) => ({ posX: state.posX, posY: state.posY }),
    },
  ),
);
