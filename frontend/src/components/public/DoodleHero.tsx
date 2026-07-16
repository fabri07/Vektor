/**
 * DoodleHero — hero de la landing en tema dark, estructura estilo adhoc:
 * título grande a la IZQUIERDA + collage de doodles blancos a la DERECHA.
 *
 * Los doodles son SVG inline (`./doodles`) generados en Figma. Se embeben inline
 * — no vía <Image> — para que el CSS pueda animar el "dibujado" del trazo
 * (`.doodle-path`) además del fade + flotar del contenedor (`.doodle-anim`).
 * Todo respeta `prefers-reduced-motion` (ver globals.css).
 */

import Link from "next/link";
import {
  DoodleChat,
  DoodleCrecimiento,
  DoodleDeco,
  DoodleKiosco,
  DoodleLimpieza,
  DoodlePersonaLaptop,
} from "./doodles";

// Posiciones del collage (en %). `delay` escalona la entrada (fade) del doodle;
// `drawDelay` arranca el "dibujado" de sus trazos en sincronía con esa entrada.
const DOODLES = [
  { Comp: DoodlePersonaLaptop, style: "left-[4%] top-[6%] w-[44%]", delay: 0 },
  { Comp: DoodleKiosco, style: "right-[2%] top-[0%] w-[36%]", delay: 0.12 },
  { Comp: DoodleCrecimiento, style: "right-[6%] top-[42%] w-[32%]", delay: 0.24 },
  { Comp: DoodleLimpieza, style: "left-[0%] top-[52%] w-[30%]", delay: 0.36 },
  { Comp: DoodleDeco, style: "left-[34%] top-[58%] w-[32%]", delay: 0.48 },
  { Comp: DoodleChat, style: "right-[32%] top-[8%] w-[28%]", delay: 0.6 },
];

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
              href="/register?src=hero_primary"
              className="inline-flex items-center justify-center rounded-full bg-white px-8 py-3.5 text-sm font-semibold text-vektor-night transition hover:-translate-y-0.5 hover:bg-white/90"
            >
              Empezar gratis
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
        <div
          aria-hidden
          className="relative mx-auto aspect-square w-full max-w-[520px]"
        >
          {DOODLES.map(({ Comp, style, delay }, i) => (
            <div
              key={i}
              className={`doodle-anim absolute ${style}`}
              style={{ animationDelay: `${delay}s` }}
            >
              <Comp drawDelay={delay} />
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
