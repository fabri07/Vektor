"use client";

import { type ReactNode, useEffect, useRef } from "react";

/**
 * Un menú de la cabecera de la caja (Impresora, Lector, Sin internet).
 *
 * Sigue siendo un `<details>` —abre y cierra sin estado propio—, pero además se
 * cierra con Escape y con un clic afuera. Hasta 2026-10 sólo se cerraba con su
 * propio rótulo, y abierto tapaba el buscador.
 */
export function MenuCabecera({ titulo, children }: { titulo: string; children: ReactNode }) {
  const ref = useRef<HTMLDetailsElement>(null);

  useEffect(() => {
    const cerrar = () => ref.current?.removeAttribute("open");
    const alClic = (e: PointerEvent) => {
      const el = ref.current;
      if (el?.open && e.target instanceof Node && !el.contains(e.target)) cerrar();
    };
    const alTeclear = (e: KeyboardEvent) => {
      const el = ref.current;
      if (e.key !== "Escape" || !el?.open) return;
      cerrar();
      el.querySelector("summary")?.focus();
    };
    document.addEventListener("pointerdown", alClic);
    document.addEventListener("keydown", alTeclear);
    return () => {
      document.removeEventListener("pointerdown", alClic);
      document.removeEventListener("keydown", alTeclear);
    };
  }, []);

  return (
    <details ref={ref} className="relative">
      <summary className="cursor-pointer text-pos-text-2">{titulo}</summary>
      {children}
    </details>
  );
}
