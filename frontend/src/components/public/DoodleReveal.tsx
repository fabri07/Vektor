"use client";

/**
 * Retiene el "dibujado" de un doodle hasta que entra en pantalla.
 *
 * Los doodles se dibujan solos al cargar (`.doodle-path` en globals.css). En el
 * hero eso está bien —se ve—, pero en una página larga como `/rubros` los seis
 * se dibujarían a la vez arriba de todo y el visitante llegaría a cada uno con
 * la animación ya terminada.
 *
 * **La retención la aplica el cliente, nunca el HTML del servidor.** Si la clase
 * `doodle-hold` viniera en el markup, un visitante sin JS se quedaría con seis
 * doodles invisibles (`stroke-dashoffset: 1` y sin animación que lo lleve a 0) —
 * cambiar una animación por contenido faltante es un mal negocio. Sin JS, todo
 * se dibuja al cargar, que es el comportamiento que ya existía.
 *
 * Solo se retiene lo que está ENTERAMENTE debajo del pliegue al montar: si ya se
 * está dibujando a la vista, retenerlo produciría un parpadeo.
 */

import { useEffect, useRef, type ReactNode } from "react";

export function DoodleReveal({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === "undefined") return;
    if (el.getBoundingClientRect().top <= window.innerHeight) return;

    el.classList.add("doodle-hold");
    const observador = new IntersectionObserver(
      (entradas) => {
        for (const entrada of entradas) {
          if (!entrada.isIntersecting) continue;
          entrada.target.classList.add("is-visible");
          observador.unobserve(entrada.target);
        }
      },
      // Espera a que el doodle esté bien adentro del viewport, no asomando por
      // el borde: el trazo tarda 1.2s y conviene que arranque cuando ya se lee.
      { rootMargin: "0px 0px -20% 0px" },
    );
    observador.observe(el);
    return () => observador.disconnect();
  }, []);

  return (
    <div ref={ref} className={className}>
      {children}
    </div>
  );
}
