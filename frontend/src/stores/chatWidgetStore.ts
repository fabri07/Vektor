"use client";

import { create } from "zustand";
import { persist } from "zustand/middleware";

interface ChatWidgetState {
  isOpen: boolean;
  /** Posición del ícono flotante (px desde la esquina sup-izq). null = default abajo-derecha. */
  posX: number | null;
  posY: number | null;
  open: () => void;
  close: () => void;
  toggle: () => void;
  setPosition: (x: number, y: number) => void;
}

export const useChatWidgetStore = create<ChatWidgetState>()(
  persist(
    (set) => ({
      isOpen: false,
      posX: null,
      posY: null,
      open: () => set({ isOpen: true }),
      close: () => set({ isOpen: false }),
      toggle: () => set((s) => ({ isOpen: !s.isOpen })),
      setPosition: (x, y) => set({ posX: x, posY: y }),
    }),
    {
      name: "vektor_chat_widget",
      // No persistimos isOpen — el widget arranca cerrado en cada sesión.
      partialize: (state) => ({ posX: state.posX, posY: state.posY }),
    },
  ),
);
