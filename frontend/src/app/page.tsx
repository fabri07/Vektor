"use client";

import { useEffect } from "react";
import Image from "next/image";
import { useRouter } from "next/navigation";
import {
  Activity,
  Boxes,
  Truck,
  ShieldCheck,
} from "lucide-react";
import { useAuthStore } from "@/stores/authStore";
import { VektorLogo } from "@/components/ui/VektorLogo";

const SOCIAL_PROOF_CARDS = [
  { seed: 11, caption: "Kiosco en Rosario" },
  { seed: 12, caption: "Distribuidora en CABA" },
  { seed: 13, caption: "Negocio familiar en Córdoba" },
  { seed: 14, caption: "Local barrial en Mendoza" },
  { seed: 15, caption: "Mayorista en La Plata" },
  { seed: 16, caption: "Tienda de cercanía en Salta" },
  { seed: 17, caption: "Autoservicio en Tucumán" },
  { seed: 18, caption: "Despensa en Mar del Plata" },
];

const FEATURE_HIGHLIGHTS = [
  {
    icon: Activity,
    title: "Salud financiera en tiempo real",
    description:
      "Entendé cómo viene tu caja, qué riesgos están creciendo y cuál es la próxima acción concreta para proteger el negocio.",
  },
  {
    icon: Boxes,
    title: "Control de inventario inteligente",
    description:
      "Detectá quiebres, exceso de mercadería y oportunidades de rotación antes de que se conviertan en plata inmovilizada.",
  },
  {
    icon: Truck,
    title: "Gestión de proveedores centralizada",
    description:
      "Concentrá compras, condiciones de pago y dependencia por proveedor para negociar mejor y evitar cuellos de botella.",
  },
];

function Navbar() {
  return (
    <nav className="fixed inset-x-0 top-0 z-50 border-b border-vektor-border/80 bg-vektor-night/80 backdrop-blur-xl">
      <div className="mx-auto flex h-16 max-w-7xl items-center justify-between px-6">
        <VektorLogo variant="full" size="md" theme="dark" />
        <div className="flex items-center gap-3">
          <a
            href="/login"
            className="hidden rounded-full border border-vektor-border px-4 py-2 text-sm font-medium text-vektor-body hover:border-vektor-blue/50 hover:bg-vektor-surface sm:inline-flex"
          >
            Iniciar sesión
          </a>
          <a
            href="/register"
            className="inline-flex rounded-full bg-gradient-to-r from-vektor-blue to-vektor-teal px-5 py-2.5 text-sm font-semibold text-vektor-white shadow-glow"
          >
            Empezar gratis
          </a>
        </div>
      </div>
    </nav>
  );
}

function Hero() {
  return (
    <section className="relative flex min-h-screen items-center overflow-hidden bg-vektor-night pt-16">
      <div className="pointer-events-none absolute inset-0">
        <div className="animate-mesh absolute inset-0 bg-[radial-gradient(circle_at_20%_20%,rgba(58,134,255,0.25),transparent_28%),radial-gradient(circle_at_80%_18%,rgba(39,199,184,0.18),transparent_22%),radial-gradient(circle_at_50%_80%,rgba(241,182,72,0.08),transparent_18%)]" />
        <div className="absolute inset-0 bg-[linear-gradient(135deg,rgba(255,255,255,0.03)_0%,transparent_30%,rgba(255,255,255,0.02)_100%)]" />
      </div>

      <div className="relative mx-auto flex max-w-7xl flex-col items-center px-6 py-24 text-center">
        <div className="inline-flex items-center gap-2 rounded-full border border-vektor-border bg-vektor-ink/80 px-4 py-2 text-xs font-medium uppercase tracking-[0.18em] text-vektor-muted">
          <ShieldCheck className="h-4 w-4 text-vektor-teal" />
          Decisiones más claras para PYMEs argentinas
        </div>

        <h1 className="mt-8 text-balance font-display text-[40px] font-bold leading-[0.95] text-vektor-white md:text-[72px]">
          Tomá las mejores decisiones
          <br />
          para tu negocio.
        </h1>

        <p className="mt-6 max-w-3xl text-[18px] leading-8 text-vektor-muted md:text-[20px]">
          Véktor analiza tu caja, inventario y proveedores — y te dice exactamente qué hacer.
        </p>

        <div className="mt-10 flex flex-col items-center gap-4 sm:flex-row">
          <a
            href="/register"
            className="inline-flex items-center justify-center rounded-full bg-gradient-to-r from-vektor-blue to-vektor-teal px-8 py-4 text-base font-semibold text-vektor-white shadow-glow hover:-translate-y-0.5"
          >
            Empezar gratis
          </a>
          <a
            href="/demo"
            className="group inline-flex items-center gap-2 text-base font-medium text-vektor-body"
          >
            <span className="relative">
              Ver demo →
              <span className="absolute inset-x-0 bottom-[-4px] h-px origin-left scale-x-0 bg-vektor-body transition-transform duration-200 group-hover:scale-x-100" />
            </span>
          </a>
        </div>
      </div>
    </section>
  );
}

function SocialProofStrip() {
  const cards = [...SOCIAL_PROOF_CARDS, ...SOCIAL_PROOF_CARDS];

  return (
    <section className="overflow-hidden border-y border-vektor-border bg-vektor-night py-8">
      {/* TODO: Replace picsum URLs with real customer photos */}
      <div className="group flex min-w-max animate-marquee gap-5 px-6 hover:[animation-play-state:paused]">
        {cards.map((card, index) => (
          <article
            key={`${card.seed}-${index}`}
            className="group/card relative h-[220px] w-[300px] overflow-hidden rounded-2xl border border-vektor-border bg-vektor-ink shadow-lg"
          >
            <Image
              src={`https://picsum.photos/seed/${card.seed}/400/300`}
              alt={card.caption}
              fill
              sizes="300px"
              className="object-cover transition-all duration-300 group-hover/card:scale-[1.03] group-hover/card:shadow-glow"
            />
            <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-vektor-night via-vektor-night/70 to-transparent px-4 pb-4 pt-10">
              <p className="text-sm font-medium text-white">{card.caption}</p>
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}

function FeatureHighlights() {
  return (
    <section className="bg-vektor-night py-24">
      <div className="mx-auto max-w-7xl px-6">
        <div className="mb-12 max-w-3xl">
          <p className="text-sm font-medium uppercase tracking-[0.16em] text-vektor-teal">
            Qué resuelve Véktor
          </p>
          <h2 className="mt-4 font-display text-4xl font-semibold text-vektor-white">
            Un tablero pensado para dueños de negocio, no para técnicos.
          </h2>
        </div>

        <div className="grid gap-6 md:grid-cols-3">
          {FEATURE_HIGHLIGHTS.map((feature) => {
            const Icon = feature.icon;

            return (
              <article
                key={feature.title}
                className="vektor-card p-7"
              >
                <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-vektor-surface text-vektor-teal">
                  <Icon className="h-6 w-6" />
                </div>
                <h3 className="mt-6 font-display text-2xl font-semibold text-vektor-white">
                  {feature.title}
                </h3>
                <p className="mt-3 text-base leading-7 text-vektor-muted">
                  {feature.description}
                </p>
              </article>
            );
          })}
        </div>
      </div>
    </section>
  );
}

function WorkflowPreview() {
  return (
    <section className="bg-vektor-night pb-28">
      <div className="mx-auto max-w-7xl px-6">
        <div className="vektor-card overflow-hidden p-8">
          <div className="flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
            <div>
              <p className="text-sm font-medium uppercase tracking-[0.16em] text-vektor-blue">
                Vista previa
              </p>
              <h2 className="mt-3 font-display text-4xl font-semibold text-vektor-white">
                Todo lo importante en una sola pantalla.
              </h2>
            </div>
            <p className="max-w-xl text-sm leading-7 text-vektor-muted">
              Caja, margen, stock, proveedores y salud general del negocio en una experiencia oscura, clara y accionable.
            </p>
          </div>

          {/* TODO: Replace with actual dashboard screenshot */}
          <div className="mt-8 rounded-[28px] border border-vektor-border bg-[radial-gradient(circle_at_top_left,rgba(58,134,255,0.22),transparent_26%),linear-gradient(135deg,#101a2d_0%,#13223a_48%,#0c1422_100%)] p-8">
            <div className="flex items-center gap-2">
              <span className="h-3 w-3 rounded-full bg-vektor-red" />
              <span className="h-3 w-3 rounded-full bg-vektor-amber" />
              <span className="h-3 w-3 rounded-full bg-vektor-teal" />
            </div>
            <div className="mt-10 grid gap-4 lg:grid-cols-[1.2fr_0.8fr]">
              <div className="rounded-2xl border border-vektor-border bg-vektor-ink/80 p-6">
                <p className="text-sm uppercase tracking-[0.16em] text-vektor-muted">
                  Dashboard Preview
                </p>
                <div className="mt-6 h-56 rounded-2xl bg-[linear-gradient(135deg,rgba(58,134,255,0.15),rgba(39,199,184,0.08)),linear-gradient(180deg,rgba(255,255,255,0.02),transparent)]" />
              </div>
              <div className="grid gap-4">
                <div className="rounded-2xl border border-vektor-border bg-vektor-ink/80 p-6" />
                <div className="rounded-2xl border border-vektor-border bg-vektor-ink/80 p-6" />
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

function Footer() {
  return (
    <footer className="border-t border-vektor-border bg-vektor-night py-10">
      <div className="mx-auto flex max-w-7xl flex-col gap-6 px-6 md:flex-row md:items-center md:justify-between">
        <div>
          <VektorLogo variant="wordmark" size="md" theme="dark" />
          <p className="mt-2 text-sm text-vektor-muted">
            Véktor te ayuda a decidir mejor sin vivir en planillas.
          </p>
        </div>
        <div className="flex flex-wrap gap-5 text-sm text-vektor-muted">
          <a href="/privacidad" className="hover:text-vektor-white">Privacidad</a>
          <a href="/terminos" className="hover:text-vektor-white">Términos</a>
          <a href="/contacto" className="hover:text-vektor-white">Contacto</a>
        </div>
      </div>
    </footer>
  );
}

export default function RootPage() {
  const router = useRouter();
  const token = useAuthStore((s) => s.token);
  const hasHydrated = useAuthStore((s) => s._hasHydrated);

  useEffect(() => {
    if (hasHydrated && token) {
      router.replace("/chat");
    }
  }, [hasHydrated, token, router]);

  return (
    <>
      <Navbar />
      <Hero />
      <SocialProofStrip />
      <FeatureHighlights />
      <WorkflowPreview />
      <Footer />
    </>
  );
}
