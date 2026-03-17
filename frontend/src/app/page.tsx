"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAuthStore } from "@/stores/authStore";

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
    <nav className="fixed inset-x-0 top-0 z-50 border-b border-white/5 bg-[#0F1623]/90 backdrop-blur-md">
      <div className="mx-auto flex h-16 max-w-6xl items-center justify-between px-6">
        <span className="text-lg font-bold tracking-tight text-white">VÉKTOR</span>
        <div className="flex items-center gap-3">
          <a
            href="/login"
            className="hidden rounded-lg border border-white/20 px-4 py-2 text-sm font-medium text-white transition-colors hover:border-white/40 hover:bg-white/5 sm:block focus:outline-none focus:ring-2 focus:ring-white/20"
          >
            Iniciar sesión
          </a>
          <a
            href="/register"
            className="rounded-lg bg-[#2B7FD4] px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-[#1E6BB8] focus:outline-none focus:ring-2 focus:ring-[#2B7FD4]/40"
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
      className="relative flex min-h-screen items-center bg-[#0F1623] pt-16"
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
          className="font-bold text-[#E8EDF4]"
          style={{ fontSize: "clamp(2rem, 5vw, 3.5rem)", lineHeight: 1.15 }}
        >
          Tu negocio claro,
          <br />
          todos los días.
        </h1>
        <p className="mx-auto mt-6 max-w-xl text-[18px] leading-relaxed text-[#8A9BB0]">
          Véktor analiza tu caja, margen y stock en tiempo real.
          <br className="hidden sm:block" />
          Sin contabilidad. Sin hojas de cálculo. Sin sorpresas.
        </p>
        <div className="mt-10 flex flex-col items-center gap-4 sm:flex-row sm:justify-center">
          <a
            href="/register"
            className="w-full rounded-xl bg-[#2B7FD4] px-8 py-4 text-base font-semibold text-white transition-colors hover:bg-[#1E6BB8] focus:outline-none focus:ring-2 focus:ring-[#2B7FD4]/40 sm:w-auto"
          >
            Empezar gratis
          </a>
          <a
            href="/demo"
            className="w-full rounded-xl border border-white/20 px-8 py-4 text-base font-medium text-white transition-colors hover:border-white/40 hover:bg-white/5 focus:outline-none focus:ring-2 focus:ring-white/20 sm:w-auto"
          >
            Ver demo
          </a>
        </div>
      </div>
    </section>
  );
}

function TrustBand() {
  return (
    <div className="border-y border-white/10 bg-[#131c2e] py-5">
      <div className="mx-auto max-w-4xl px-6">
        <ul className="flex flex-col items-center gap-4 text-sm text-[#8A9BB0] sm:flex-row sm:justify-center sm:gap-10">
          {TRUST_ITEMS.map((item) => (
            <li key={item} className="flex items-center gap-2">
              <span className="text-[#2B7FD4]" aria-hidden="true">✓</span>
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
    <section className="bg-[#F7F8FA] py-24">
      <div className="mx-auto max-w-5xl px-6">
        <h2 className="mb-16 text-center text-3xl font-bold text-[#1A2744]">
          Cómo funciona
        </h2>
        <div className="grid gap-10 sm:grid-cols-3">
          {HOW_IT_WORKS.map((step) => (
            <div key={step.num} className="flex flex-col">
              <span className="mb-4 text-5xl font-bold text-[#2B7FD4] leading-none">
                {step.num}
              </span>
              <h3 className="mb-2 text-lg font-semibold text-[#1A2744]">{step.title}</h3>
              <p className="text-sm leading-relaxed text-gray-500">{step.desc}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function Verticals() {
  return (
    <section className="bg-white py-24">
      <div className="mx-auto max-w-5xl px-6">
        <h2 className="mb-4 text-center text-3xl font-bold text-[#1A2744]">
          Hecho para tu rubro
        </h2>
        <p className="mb-16 text-center text-gray-500">
          Véktor conoce los riesgos específicos de cada negocio.
        </p>
        <div className="grid gap-6 sm:grid-cols-3">
          {VERTICALS.map((v) => (
            <div
              key={v.name}
              className="rounded-2xl border border-[#E5E9F0] p-6 transition-shadow hover:shadow-md"
            >
              <h3 className="mb-3 text-base font-semibold text-[#1A2744]">{v.name}</h3>
              <p className="text-[13px] leading-relaxed text-gray-500">
                <span className="mr-1 font-medium text-[#2B7FD4]">Riesgo principal:</span>
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
    <footer className="bg-[#0F1623] py-12">
      <div className="mx-auto max-w-5xl px-6">
        <div className="flex flex-col items-center gap-6 sm:flex-row sm:justify-between">
          <div>
            <span className="text-lg font-bold tracking-tight text-white">VÉKTOR</span>
            <p className="mt-1 text-xs text-[#8A9BB0]">No trabajes más. Decidí mejor.</p>
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
                className="text-xs text-[#8A9BB0] transition-colors hover:text-white focus:outline-none focus:underline"
              >
                {label}
              </a>
            ))}
          </nav>
        </div>
        <p className="mt-8 text-center text-xs text-[#8A9BB0]/60">
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
