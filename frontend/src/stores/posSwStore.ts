import { create } from "zustand";
import { persist } from "zustand/middleware";

/**
 * "Preparar esta PC para abrir sin internet" (B7b).
 *
 * Por PC y no por variable de entorno: `NEXT_PUBLIC_*` se inlinea en el build,
 * así que apagarlo exigiría recompilar y desplegar — justo lo que un kill
 * switch no puede necesitar. Con esto, una PC que nunca lo encendió no tiene
 * service worker, y apagarlo lo desregistra y borra su caché en el acto.
 *
 * Apagado por defecto: encenderlo en la PC del local ES el paso 1 de B1
 * (instalar, cortar la red, reiniciar, abrir desde el ícono).
 */
interface PosSwState {
  habilitado: boolean;
  setHabilitado: (v: boolean) => void;
}

export const usePosSwStore = create<PosSwState>()(
  persist(
    (set) => ({
      habilitado: false,
      setHabilitado: (habilitado) => set({ habilitado }),
    }),
    { name: "vektor-pos-sin-internet" },
  ),
);
