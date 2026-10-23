import Link from "next/link";
import { Check } from "lucide-react";
import { PageHeader } from "@/components/public/PageHeader";

export const metadata = {
  title: "Precios | Véktor",
  description:
    "Tres planes para PYMEs argentinas: Esencial, Control y Dirección. Empezás con una prueba de 14 días, sin tarjeta.",
};

interface Plan {
  code: "esencial" | "control" | "direccion";
  name: string;
  tagline: string;
  priceUsd: number;
  priceArs: string;
  features: string[];
  featured?: boolean;
}

// Precios de referencia sin cambios respecto de la política aprobada — nunca
// se suben. ARS es ilustrativo (MEP ~1.536, referencia 17/9/2026): se
// recalcula al momento de facturar, ver la política de planes en `docs/plans/`.
const plans: Plan[] = [
  {
    code: "esencial",
    name: "Esencial",
    tagline: "Para empezar a ordenar el negocio.",
    priceUsd: 12,
    priceArs: "18.500",
    features: [
      "1 usuario",
      "20 consultas de IA por mes",
      "5 planillas por mes",
      "1 lectura de foto o PDF por mes",
      "Historial completo de tus datos",
      "Soporte por email",
    ],
  },
  {
    code: "control",
    name: "Control",
    tagline: "Para gestionar y anticiparse.",
    priceUsd: 27,
    priceArs: "41.500",
    featured: true,
    features: [
      "3 usuarios con roles",
      "70 consultas de IA por mes",
      "25 planillas por mes",
      "7 lecturas de foto o PDF por mes",
      "Pronóstico de caja y automatizaciones",
      "Soporte prioritario",
    ],
  },
  {
    code: "direccion",
    name: "Dirección",
    tagline: "Para dirección y equipo completo.",
    priceUsd: 59,
    priceArs: "90.500",
    features: [
      "5 usuarios con roles",
      "180 consultas de IA por mes",
      "75 planillas por mes",
      "18 lecturas de foto o PDF por mes",
      "Integraciones Google y automatizaciones avanzadas",
      "Soporte prioritario + incorporación guiada",
    ],
  },
];

export default function PreciosPage() {
  return (
    <>
      <PageHeader
        title="Un plan para cada etapa"
        subtitle={
          <>
            Tres planes, sin límites artificiales a lo que ya cargaste.
            Empezás con una prueba de 14 días, sin tarjeta.
          </>
        }
      />

      <section className="mx-auto max-w-5xl px-6 pb-10">
        <div className="grid gap-6 md:grid-cols-3">
          {plans.map((plan) => (
            <div
              key={plan.code}
              className={
                "vektor-card flex flex-col p-8" +
                (plan.featured ? " border-vektor-teal/50 ring-1 ring-vektor-teal/30" : "")
              }
            >
              <div className="flex items-center justify-between gap-2">
                <h2 className="font-display text-2xl font-bold uppercase tracking-tight text-vektor-white">
                  {plan.name}
                </h2>
                {plan.featured && (
                  <span className="rounded-full bg-gradient-to-r from-vektor-blue-strong to-vektor-teal-deep px-3 py-1 text-xs font-semibold uppercase tracking-wide text-white">
                    Recomendado
                  </span>
                )}
              </div>
              <p className="mt-2 text-vektor-muted">{plan.tagline}</p>
              <p className="mt-6">
                <span className="font-display text-5xl font-bold text-vektor-white">
                  USD {plan.priceUsd}
                </span>
                <span className="ml-2 text-sm text-vektor-muted">/ mes</span>
              </p>
              <p className="mt-1 text-xs text-vektor-muted">
                ≈ ARS {plan.priceArs} · referencia, se ajusta al facturar
              </p>
              <ul className="mt-6 flex-1 space-y-3">
                {plan.features.map((f) => (
                  <li key={f} className="flex items-start gap-3 text-vektor-body">
                    <Check className="mt-0.5 h-5 w-5 shrink-0 text-vektor-teal" />
                    <span>{f}</span>
                  </li>
                ))}
              </ul>
              <Link
                href={`/solicitar-acceso?plan=${plan.code}&src=precios_${plan.code}`}
                className={
                  "mt-8 inline-flex items-center justify-center rounded-full px-7 py-3 text-sm font-semibold transition " +
                  (plan.featured
                    ? "bg-gradient-to-r from-vektor-blue-strong to-vektor-teal-deep text-white hover:brightness-95"
                    : "bg-white text-vektor-night hover:bg-white/90")
                }
              >
                {/*
                  "Empezar" sobrepromete: el registro es cerrado y el alta la
                  aprueba el dueño a mano, así que acá no se empieza nada — se
                  postula (mismo criterio que dejó `precios_cta.test.tsx` tras
                  el copy pass de 2026-08-18: el verbo "pedir" describe lo que
                  realmente pasa al hacer click).
                */}
                Quiero pedir el plan {plan.name}
              </Link>
            </div>
          ))}
        </div>

        <p className="mx-auto mt-10 max-w-2xl text-center text-sm text-vektor-muted">
          Todos los planes arrancan con una prueba de 14 días, sin tarjeta. Al
          pedir acceso revisamos tu solicitud a mano para confirmar que Véktor
          pueda aportar valor a tu negocio desde el inicio — no es un alta
          automática.
        </p>
      </section>
    </>
  );
}
