/**
 * DoodleHero — hero de la landing en tema dark, estructura estilo adhoc:
 * título grande a la IZQUIERDA + collage de doodles blancos a la DERECHA.
 *
 * El collage vive en `./DoodleCollage`, compartido con el panel izquierdo del
 * login para que ambos muestren exactamente el mismo dibujo.
 */

import Link from "next/link";
import { DoodleCollage } from "./DoodleCollage";

export function DoodleHero() {
  return (
    <section className="relative overflow-hidden bg-vektor-night pt-28 pb-20 sm:pt-32">
      {/* glow de fondo sutil */}
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(ellipse_at_75%_35%,rgba(58,134,255,0.14),transparent_55%)]" />

      <div className="relative mx-auto grid max-w-7xl items-center gap-12 px-6 lg:grid-cols-2">
        {/* Título — izquierda */}
        <div className="max-w-xl">
          <p className="mb-4 text-xs font-semibold uppercase tracking-[0.22em] text-vektor-teal">
            Salud financiera para negocios argentinos
          </p>
          <h1 className="font-display text-[46px] font-bold uppercase leading-[0.95] tracking-tight text-vektor-white sm:text-[64px] lg:text-[72px]">
            Tomá las mejores decisiones.
          </h1>
          <p className="mt-6 text-lg leading-8 text-vektor-body">
            Véktor entiende tu kiosco, tu local de limpieza o tu tienda de
            decoración y te dice cómo viene la caja, qué riesgo está creciendo y
            cuál es la próxima acción concreta.
          </p>
          <div className="mt-8 flex flex-col gap-3 sm:flex-row sm:items-center sm:gap-4">
            <Link
              href="/solicitar-acceso?src=hero_primary"
              className="inline-flex items-center justify-center rounded-full bg-white px-8 py-3.5 text-sm font-semibold text-vektor-night transition hover:-translate-y-0.5 hover:bg-white/90"
            >
              Pedir acceso
            </Link>
            <Link
              href="/login?src=hero_secondary"
              className="inline-flex items-center justify-center rounded-full border border-white/25 px-8 py-3.5 text-sm font-semibold text-vektor-white backdrop-blur-sm transition hover:border-white/50"
            >
              Ya tengo cuenta
            </Link>
          </div>
        </div>

        {/* Doodles — derecha */}
        <DoodleCollage className="mx-auto aspect-square w-full max-w-[520px]" />
      </div>
    </section>
  );
}
