import Link from "next/link";
import { Check } from "lucide-react";
import { PageHeader } from "@/components/public/PageHeader";

export const metadata = {
  title: "Precios | Véktor",
  description:
    "Pedí acceso a Véktor. Plan Gratuito con lo esencial y plan Premium sin límites, con beneficios a definir próximamente.",
};

const gratuitoFeatures = [
  "Consultas al asistente con límites de uso",
  "Vista esencial de salud financiera",
  "Acceso para una persona",
];

const premiumFeatures = [
  "Mayor capacidad de consultas",
  "Acceso a módulos avanzados",
  "Trabajo con más de un usuario",
  "Atención prioritaria",
];

export default function PreciosPage() {
  return (
    <>
      <PageHeader
        title="Empezá sin costo"
        subtitle={
          <>
            Probá Véktor con tu negocio real. Cuando necesites más alcance,
            vas a poder avanzar a Premium.
          </>
        }
      />

      <section className="mx-auto max-w-4xl px-6 pb-16">
        <div className="grid gap-6 md:grid-cols-2">
          {/* Gratuito */}
          <div className="vektor-card flex flex-col p-8">
            <h2 className="font-display text-2xl font-bold uppercase tracking-tight text-vektor-white">
              Gratuito
            </h2>
            <p className="mt-2 text-vektor-muted">
              Para transformar tus primeros datos en una lectura útil.
            </p>
            <p className="mt-6">
              <span className="font-display text-5xl font-bold text-vektor-white">
                $0
              </span>
            </p>
            <ul className="mt-6 flex-1 space-y-3">
              {gratuitoFeatures.map((f) => (
                <li key={f} className="flex items-start gap-3 text-vektor-body">
                  <Check className="mt-0.5 h-5 w-5 shrink-0 text-vektor-teal" />
                  <span>{f}</span>
                </li>
              ))}
            </ul>
            <Link
              href="/solicitar-acceso?plan=free&src=precios_free"
              className="mt-8 inline-flex items-center justify-center rounded-full bg-white px-7 py-3 text-sm font-semibold text-vektor-night transition hover:bg-white/90"
            >
              {/*
                "Empezar gratis" sobrepromete: el registro es cerrado y el alta
                la aprueba el dueño a mano, así que acá no se empieza nada — se
                postula. El copy pass de 2026-08-18 lo puso como "Quiero empezar
                gratis" y `precios_cta.test.tsx` lo detectó; mantener el verbo
                "pedir" es lo que hace que el CTA describa lo que realmente pasa
                al hacer click.
              */}
              Quiero pedir mi acceso gratuito
            </Link>
          </div>

          {/* Premium */}
          <div className="vektor-card flex flex-col border-vektor-teal/40 p-8">
            <div className="flex items-center justify-between">
              <h2 className="font-display text-2xl font-bold uppercase tracking-tight text-vektor-white">
                Premium
              </h2>
              <span className="rounded-full bg-gradient-to-r from-vektor-blue-strong to-vektor-teal-deep px-3 py-1 text-xs font-semibold uppercase tracking-wide text-white">
                En preparación
              </span>
            </div>
            <p className="mt-2 text-vektor-muted">
              Más capacidad para negocios que necesitan profundizar el control.
            </p>
            <p className="mt-6">
              <span className="font-display text-5xl font-bold text-vektor-white">
                Próximamente
              </span>
            </p>
            <ul className="mt-6 flex-1 space-y-3">
              {premiumFeatures.map((f) => (
                <li key={f} className="flex items-start gap-3 text-vektor-body">
                  <Check className="mt-0.5 h-5 w-5 shrink-0 text-vektor-teal" />
                  <span>{f}</span>
                </li>
              ))}
            </ul>
            <p className="mt-4 text-xs text-vektor-muted">
              Las funciones y los límites finales se confirmarán antes del
              lanzamiento.
            </p>
            <Link
              href="/solicitar-acceso?plan=premium&src=precios_premium"
              className="mt-8 inline-flex items-center justify-center rounded-full bg-gradient-to-r from-vektor-blue-strong to-vektor-teal-deep px-7 py-3 text-sm font-semibold text-white transition hover:brightness-95"
            >
              Quiero recibir novedades de Premium
            </Link>
          </div>
        </div>

        <p className="mx-auto mt-8 max-w-2xl text-center text-sm text-vektor-muted">
          Premium todavía no está disponible. Mientras terminamos de
          definirlo, podés pedir acceso gratuito. Revisamos cada solicitud
          para confirmar que Véktor pueda aportar valor a tu negocio desde el
          inicio.
        </p>
      </section>
    </>
  );
}
