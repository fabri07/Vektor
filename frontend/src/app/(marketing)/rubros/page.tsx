/**
 * `/rubros` — qué mira Véktor en cada uno de los seis rubros.
 *
 * Se rehízo al pasar de 3 a 6. Tres decisiones que conviene no revertir:
 *
 * 1. **Las secciones se derivan de `lib/rubros.ts`.** Antes eran tres bloques
 *    escritos a mano con el mismo marcado copiado; con seis eso se vuelve
 *    ingobernable y el menú "Rubros" del nav repetía la lista por su cuenta.
 *    Ahora los dos leen la misma fuente y no pueden divergir.
 * 2. **Índice arriba, secciones abajo.** Al visitante le importa SU rubro, no
 *    los seis. Una grilla de seis tarjetas iguales lo obliga a leer todo para
 *    encontrar el suyo; el índice lo lleva en un clic y deja que cada rubro
 *    tenga una sección de verdad en vez de una tarjeta apretada.
 * 3. **Sin rótulo arriba del título.** El `PARA TU RUBRO` que había no decía
 *    nada que el título no dijera mejor.
 *
 * Las anclas (`#kiosco`, `#limpieza`, `#deco`, …) son contrato: el nav público
 * enlaza a ellas desde todo el sitio. Salen de `VerticalOption.anchor`.
 */

import Link from "next/link";

import { DoodleReveal } from "@/components/public/DoodleReveal";
import { RUBROS } from "@/lib/rubros";

export const metadata = {
  title: "Rubros | Véktor",
  description:
    "Véktor entiende seis rubros: kiosco, limpieza, decoración del hogar, librería, indumentaria y verdulería. Umbrales, alertas y campos distintos para cada uno.",
};

export default function RubrosPage() {
  return (
    // Un solo contenedor para toda la página: el título, el índice y las seis
    // secciones comparten el mismo borde izquierdo. Antes el hero estaba en
    // `max-w-3xl` y el resto en `max-w-5xl`, así que el título arrancaba 160px
    // más adentro que todo lo que venía abajo.
    <div className="mx-auto max-w-5xl px-6">
      <section className="pt-20 sm:pt-28">
        <h1 className="text-balance font-display text-4xl font-bold uppercase leading-[1.05] tracking-tight text-vektor-white sm:text-6xl">
          El punto débil cambia según el rubro. Véktor también.
        </h1>
        <p className="mt-6 max-w-[58ch] text-lg leading-8 text-vektor-body">
          Un kiosco no inmoviliza plata igual que una tienda de ropa. Por eso
          Véktor adapta indicadores, alertas y datos a la lógica real de cada
          actividad. Hoy entiende estos seis rubros desde adentro.
        </p>
      </section>

      {/* Índice: el visitante viene por su rubro, no por los seis.
          En teléfono va en columna (una en SE, dos desde 640): con `flex-wrap`
          quedaba desparejo —dos ítems, uno, dos, uno— porque los nombres miden
          distinto, y un índice que no se barre de un vistazo no sirve de índice.
          El `py-3` lleva el tocable de 24 a 47px: con `py-2.5` daba 43, uno
          corto del mínimo de 44 —medido, no estimado—. */}
      <nav aria-label="Ir a un rubro" className="pt-12">
        <ul className="grid gap-x-7 sm:grid-cols-2 lg:flex lg:flex-wrap lg:gap-y-1">
          {RUBROS.map(({ code, name, anchor, Icon }) => (
            <li key={code}>
              <Link
                href={`#${anchor}`}
                className="group inline-flex items-center gap-2.5 rounded-md py-3 text-sm text-vektor-body transition-colors hover:text-vektor-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-vektor-teal focus-visible:ring-offset-4 focus-visible:ring-offset-vektor-night"
              >
                <span className="text-vektor-muted transition-colors group-hover:text-vektor-teal">
                  <Icon />
                </span>
                <span className="underline decoration-vektor-border decoration-1 underline-offset-4 transition-colors group-hover:decoration-vektor-teal">
                  {name}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      </nav>

      <div className="mt-16">
        {RUBROS.map(({ code, name, anchor, Doodle, tension, respuesta, capacidades }, i) => (
          <section
            key={code}
            id={anchor}
            className="scroll-mt-28 border-t border-vektor-border/70 py-14 sm:py-20"
          >
            <div className="grid items-start gap-8 md:grid-cols-12 md:gap-14">
              <div
                // `self-center` en desktop: el doodle mide ~260px y la sección
                // ~530, así que anclado arriba dejaba un vacío de 270px debajo.
                // En mobile queda arriba del texto, que es el orden de lectura.
                className={`md:col-span-4 md:self-center ${i % 2 === 1 ? "md:order-2" : ""}`}
                aria-hidden="true"
              >
                <DoodleReveal className="w-32 sm:w-40 md:w-full md:max-w-[260px]">
                  <Doodle />
                </DoodleReveal>
              </div>

              <div className="md:col-span-8">
                <h2 className="font-display text-3xl font-bold uppercase tracking-tight text-vektor-white sm:text-4xl">
                  {name}
                </h2>
                <p className="mt-4 max-w-[46ch] text-balance text-xl leading-9 text-vektor-white sm:text-2xl">
                  {tension}
                </p>
                <p className="mt-5 max-w-[64ch] leading-8 text-vektor-body">{respuesta}</p>

                <ul className="mt-9 max-w-[52ch]">
                  {capacidades.map((capacidad) => (
                    <li
                      key={capacidad}
                      className="border-t border-vektor-border/50 py-3 text-sm leading-6 text-vektor-muted first:border-t-0 first:pt-0"
                    >
                      {capacidad}
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          </section>
        ))}
      </div>

      <section className="border-t border-vektor-border/70 py-20">
        <p className="max-w-[52ch] text-lg leading-8 text-vektor-body">
          ¿Tu rubro todavía no está? Contanos cómo se mueve tu negocio. Si
          Véktor puede leerlo con precisión, queremos demostrártelo.
        </p>
        <Link
          href="/solicitar-acceso?src=rubros"
          className="mt-8 inline-flex items-center justify-center rounded-full bg-white px-7 py-3 text-sm font-semibold text-vektor-night transition hover:bg-white/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-vektor-teal focus-visible:ring-offset-4 focus-visible:ring-offset-vektor-night"
        >
          Quiero contarles sobre mi negocio
        </Link>
      </section>
    </div>
  );
}
