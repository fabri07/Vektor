import Link from "next/link";
import { Check } from "lucide-react";
import { PageHeader } from "@/components/public/PageHeader";

export const metadata = {
  title: "Precios | Véktor",
  description:
    "Empezá gratis con Véktor. Plan Gratuito con lo esencial y plan Premium sin límites, con beneficios a definir próximamente.",
};

const gratuitoFeatures = [
  "Acceso al chat con IA, con límite de mensajes y tokens",
  "Dashboard de salud financiera básico",
  "1 usuario",
];

const premiumFeatures = [
  "Mensajes y tokens ampliados",
  "Todos los módulos habilitados",
  "Multiusuario para tu equipo",
  "Soporte prioritario",
];

export default function PreciosPage() {
  return (
    <>
      <PageHeader
        title="Precios simples"
        subtitle={
          <>
            Empezá gratis y pasate a Premium cuando tu negocio lo pida. Sin
            letra chica.
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
              Ideal para empezar a ordenar tu negocio.
            </p>
            <p className="mt-6">
              <span className="font-display text-5xl font-bold text-vektor-white">
                Gratis
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
              href="/register"
              className="mt-8 inline-flex items-center justify-center rounded-full bg-white px-7 py-3 text-sm font-semibold text-vektor-night transition hover:bg-white/90"
            >
              Empezar gratis
            </Link>
          </div>

          {/* Premium */}
          <div className="vektor-card flex flex-col border-vektor-teal/40 p-8">
            <div className="flex items-center justify-between">
              <h2 className="font-display text-2xl font-bold uppercase tracking-tight text-vektor-white">
                Premium
              </h2>
              <span className="rounded-full bg-gradient-to-r from-vektor-blue to-vektor-teal px-3 py-1 text-xs font-semibold uppercase tracking-wide text-white">
                Pronto
              </span>
            </div>
            <p className="mt-2 text-vektor-muted">
              Todo sin límites, para cuando quieras exprimir Véktor al máximo.
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
              Beneficios y límites sujetos a definición.
            </p>
            <Link
              href="/contacto"
              className="mt-8 inline-flex items-center justify-center rounded-full bg-gradient-to-r from-vektor-blue to-vektor-teal px-7 py-3 text-sm font-semibold text-white transition hover:opacity-90"
            >
              Hablar con nosotros
            </Link>
          </div>
        </div>

        <p className="mx-auto mt-8 max-w-2xl text-center text-sm text-vektor-muted">
          Las condiciones, precios y beneficios definitivos se anunciarán
          pronto. Mientras tanto, podés arrancar con el plan Gratuito sin costo.
        </p>
      </section>
    </>
  );
}
