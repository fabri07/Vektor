"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAuthStore } from "@/stores/authStore";
import { VektorLogo } from "@/components/ui/VektorLogo";

// ── Data ─────────────────────────────────────────────────────────────────────

const HOW_IT_WORKS = [
  {
    num: "01",
    title: "Contanos tu negocio",
    desc: "3 minutos, sin datos exactos. Solo tu rubro y una estimación.",
  },
  {
    num: "02",
    title: "Véktor analiza todo",
    desc: "Score de salud, nivel de riesgo y la próxima acción concreta.",
  },
  {
    num: "03",
    title: "Decidís mejor",
    desc: "Cada semana recibís un resumen claro. Sin sorpresas.",
  },
];

const VERTICALS = [
  {
    name: "Kiosco / Almacén",
    risk: "Rotación de stock lenta y margen por debajo del punto de equilibrio.",
  },
  {
    name: "Decoración del hogar",
    risk: "Estacionalidad no contemplada que genera exceso de inventario.",
  },
  {
    name: "Limpieza",
    risk: "Dependencia de un solo proveedor con alta variabilidad de precios.",
  },
];

const TRUST_ITEMS = [
  "Tus datos bajo tu control",
  "Diseñado para PYMEs argentinas",
  "Sin contabilidad obligatoria",
];

// ── Components ────────────────────────────────────────────────────────────────

function Navbar() {
  return (
    <nav className="fixed inset-x-0 top-0 z-50 border-b border-vk-border-dark/60 bg-vk-bg-dark/90 backdrop-blur-md">
      <div className="mx-auto flex h-16 max-w-6xl items-center justify-between px-6">
        <VektorLogo variant="full" size="md" theme="dark" />
        <div className="flex items-center gap-3">
          <a
            href="/login"
            className="hidden rounded-lg border border-vk-border-dark px-4 py-2 text-sm font-medium text-vk-text-light transition-colors hover:border-vk-text-muted hover:bg-vk-surface-1 sm:block focus:outline-none focus:ring-2 focus:ring-vk-blue/30"
          >
            Iniciar sesión
          </a>
          <a
            href="/register"
            className="rounded-lg bg-vk-blue px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-vk-blue-hover focus:outline-none focus:ring-2 focus:ring-vk-blue/40"
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
    <section
      className="relative flex min-h-screen items-center bg-vk-bg-dark pt-16"
      style={{
        backgroundImage: `
          linear-gradient(rgba(255,255,255,0.03) 1px, transparent 1px),
          linear-gradient(90deg, rgba(255,255,255,0.03) 1px, transparent 1px)
        `,
        backgroundSize: "60px 60px",
      }}
    >
      <div className="mx-auto max-w-4xl px-6 py-24 text-center">
        <h1
          className="font-bold text-vk-text-light"
          style={{ fontSize: "clamp(2rem, 5vw, 3.5rem)", lineHeight: 1.15 }}
        >
          La IA que cuida la salud
          <br />
          de tu negocio
        </h1>
        <p className="mx-auto mt-6 max-w-xl text-[18px] leading-relaxed text-vk-text-muted">
          Véktor es tu asistente de IA que analiza tus ventas, gastos, stock y
          muchas cosas más en tiempo real.
        </p>
        <div className="mt-10 flex flex-col items-center gap-4 sm:flex-row sm:justify-center">
          <a
            href="/register"
            className="w-full rounded-xl bg-vk-blue px-8 py-4 text-base font-semibold text-white transition-colors hover:bg-vk-blue-hover focus:outline-none focus:ring-2 focus:ring-vk-blue/40 sm:w-auto"
          >
            Empezar gratis
          </a>
          <a
            href="/pricing"
            className="w-full rounded-xl border border-vk-border-dark px-8 py-4 text-base font-medium text-vk-text-light transition-colors hover:border-vk-text-muted hover:bg-vk-surface-1 focus:outline-none focus:ring-2 focus:ring-vk-blue/20 sm:w-auto"
          >
            Vektorizate
          </a>
        </div>
      </div>
    </section>
  );
}

function TrustBand() {
  return (
    <div className="border-y border-vk-border-dark bg-vk-surface-1 py-5">
      <div className="mx-auto max-w-4xl px-6">
        <ul className="flex flex-col items-center gap-4 text-sm text-vk-text-muted sm:flex-row sm:justify-center sm:gap-10">
          {TRUST_ITEMS.map((item) => (
            <li key={item} className="flex items-center gap-2">
              <span className="text-vk-blue" aria-hidden="true">✓</span>
              {item}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

function HowItWorks() {
  return (
    <section className="bg-vk-bg-light py-24">
      <div className="mx-auto max-w-5xl px-6">
        <h2 className="mb-16 text-center text-3xl font-bold text-vk-navy">
          Cómo funciona
        </h2>
        <div className="grid gap-10 sm:grid-cols-3">
          {HOW_IT_WORKS.map((step) => (
            <div key={step.num} className="flex flex-col">
              <span className="mb-4 text-5xl font-bold text-vk-blue leading-none">
                {step.num}
              </span>
              <h3 className="mb-2 text-lg font-semibold text-vk-navy">{step.title}</h3>
              <p className="text-sm leading-relaxed text-vk-text-secondary">{step.desc}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function Verticals() {
  return (
    <section className="bg-vk-surface-w py-24">
      <div className="mx-auto max-w-5xl px-6">
        <h2 className="mb-4 text-center text-3xl font-bold text-vk-navy">
          Hecho para tu rubro
        </h2>
        <p className="mb-16 text-center text-vk-text-secondary">
          Véktor conoce los riesgos específicos de cada negocio.
        </p>
        <div className="grid gap-6 sm:grid-cols-3">
          {VERTICALS.map((v) => (
            <div
              key={v.name}
              className="rounded-2xl border border-vk-border-w p-6 transition-shadow hover:shadow-vk-md"
            >
              <h3 className="mb-3 text-base font-semibold text-vk-navy">{v.name}</h3>
              <p className="text-[13px] leading-relaxed text-vk-text-secondary">
                <span className="mr-1 font-medium text-vk-blue">Riesgo principal:</span>
                {v.risk}
              </p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function Footer() {
  return (
    <footer className="bg-vk-bg-dark py-12">
      <div className="mx-auto max-w-5xl px-6">
        <div className="flex flex-col items-center gap-6 sm:flex-row sm:justify-between">
          <div>
            <VektorLogo variant="wordmark" size="md" theme="dark" />
            <p className="mt-1 text-xs text-vk-text-muted">Trabaja menos y toma las mejores decisiones.</p>
          </div>
          <nav className="flex flex-wrap justify-center gap-5" aria-label="Footer">
            {[
              ["Privacidad", "/privacidad"],
              ["Términos", "/terminos"],
              ["Contacto", "/contacto"],
            ].map(([label, href]) => (
              <a
                key={label}
                href={href}
                className="text-xs text-vk-text-muted transition-colors hover:text-vk-text-light focus:outline-none focus:underline"
              >
                {label}
              </a>
            ))}
          </nav>
        </div>
        <p className="mt-8 text-center text-xs text-vk-text-muted/60">
          Véktor — Buenos Aires, Argentina
        </p>
      </div>
    </footer>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function RootPage() {
  const router = useRouter();
  const token = useAuthStore((s) => s.token);
  const hasHydrated = useAuthStore((s) => s._hasHydrated);

  useEffect(() => {
    if (hasHydrated && token) {
      router.replace("/dashboard");
    }
  }, [hasHydrated, token, router]);

  return (
    <>
      <Navbar />
      <Hero />
      <TrustBand />
      <HowItWorks />
      <Verticals />
      <Footer />
    </>
  );
}
