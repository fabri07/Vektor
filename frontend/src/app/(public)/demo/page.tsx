"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { loginRequest } from "@/services/auth.service";
import { useAuthStore } from "@/stores/authStore";
import { VektorLogo } from "@/components/ui/VektorLogo";

// ── Types ─────────────────────────────────────────────────────────────────────

interface DemoTenant {
  email: string;
  password: string;
  name: string;
  vertical: string;
  score: number;
  scoreLabel: string;
  scoreBg: string;
  scoreText: string;
  risk: string;
  riskIcon: string;
  description: string;
  highlights: string[];
}

// ── Data ─────────────────────────────────────────────────────────────────────

const DEMO_TENANTS: DemoTenant[] = [
  {
    email: "demo.kiosco@vektor.app",
    password: "Demo1234!",
    name: "Kiosco San Martín",
    vertical: "Kiosco",
    score: 74,
    scoreLabel: "Saludable",
    scoreBg: "bg-emerald-500/15",
    scoreText: "text-emerald-400",
    risk: "Dependencia de proveedor",
    riskIcon: "→",
    description: "Negocio en crecimiento con 3 semanas de mejora consecutiva.",
    highlights: [
      "Score mejoró 16 puntos en 8 semanas",
      "M1 y M2 desbloqueados",
      "$185.000 ARS valor protegido",
    ],
  },
  {
    email: "demo.limpieza@vektor.app",
    password: "Demo1234!",
    name: "Distribuidora Clean",
    vertical: "Limpieza",
    score: 51,
    scoreLabel: "En riesgo",
    scoreBg: "bg-red-500/15",
    scoreText: "text-red-400",
    risk: "Caja crítica — 17 días de cobertura",
    riskIcon: "⚠",
    description: "Score en caída. Caja insuficiente y dependencia de un solo proveedor.",
    highlights: [
      "Score bajó 12 puntos en 1 mes",
      "Caja cubre solo 17 días",
      "2 alertas críticas activas",
    ],
  },
  {
    email: "demo.deco@vektor.app",
    password: "Demo1234!",
    name: "Casa & Deco Palermo",
    vertical: "Decoración hogar",
    score: 62,
    scoreLabel: "Estable",
    scoreBg: "bg-amber-500/15",
    scoreText: "text-amber-400",
    risk: "Margen bajo benchmark del rubro",
    riskIcon: "↘",
    description: "Score estable. Margen por debajo del rubro y stock inmovilizado.",
    highlights: [
      "40% del catálogo sin rotación",
      "Margen 20.6% vs 25-45% del rubro",
      "Primera semana de mejora",
    ],
  },
];

// ── Sub-components ────────────────────────────────────────────────────────────

function DemoBanner() {
  return (
    <div className="bg-amber-500 px-4 py-3 text-center">
      <p className="text-sm font-medium text-amber-950">
        <span className="mr-2 font-bold">Modo demo</span>—
        <span className="ml-2">datos de ejemplo. No son datos reales de ningún negocio.</span>
      </p>
    </div>
  );
}

function ScoreBadge({ score, label, bg, text }: { score: number; label: string; bg: string; text: string }) {
  return (
    <div className={`inline-flex items-center gap-2 rounded-full px-3 py-1.5 ${bg}`}>
      <span className={`text-2xl font-bold tabular-nums ${text}`}>{score}</span>
      <span className={`text-xs font-medium ${text}`}>{label}</span>
    </div>
  );
}

function TenantCard({
  tenant,
  loading,
  onClick,
}: {
  tenant: DemoTenant;
  loading: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      disabled={loading}
      className="group w-full rounded-2xl border border-vk-border-dark bg-vk-surface-1 p-6 text-left transition-all duration-200 hover:border-vk-blue/40 hover:bg-vk-surface-1/80 hover:shadow-vk-md focus:outline-none focus:ring-2 focus:ring-vk-blue/40 disabled:opacity-60 disabled:cursor-not-allowed"
    >
      {/* Header */}
      <div className="mb-4 flex items-start justify-between gap-4">
        <div>
          <p className="text-xs font-medium uppercase tracking-wider text-vk-text-muted">
            {tenant.vertical}
          </p>
          <h3 className="mt-1 text-lg font-semibold text-vk-text-light">
            {tenant.name}
          </h3>
        </div>
        <ScoreBadge
          score={tenant.score}
          label={tenant.scoreLabel}
          bg={tenant.scoreBg}
          text={tenant.scoreText}
        />
      </div>

      {/* Description */}
      <p className="mb-4 text-sm leading-relaxed text-vk-text-muted">
        {tenant.description}
      </p>

      {/* Risk */}
      <div className="mb-4 flex items-center gap-2 rounded-lg bg-white/5 px-3 py-2">
        <span className="text-sm font-bold text-vk-text-muted" aria-hidden="true">
          {tenant.riskIcon}
        </span>
        <span className="text-sm text-vk-text-muted">
          <span className="font-medium text-vk-text-light">Riesgo principal: </span>
          {tenant.risk}
        </span>
      </div>

      {/* Highlights */}
      <ul className="mb-5 space-y-1.5">
        {tenant.highlights.map((h) => (
          <li key={h} className="flex items-center gap-2 text-xs text-vk-text-muted">
            <span className="text-vk-blue" aria-hidden="true">✓</span>
            {h}
          </li>
        ))}
      </ul>

      {/* CTA */}
      <div
        className={`flex items-center justify-center gap-2 rounded-xl px-4 py-3 text-sm font-semibold transition-colors ${
          loading
            ? "bg-vk-surface-1 text-vk-text-muted"
            : "bg-vk-blue text-white group-hover:bg-vk-blue-hover"
        }`}
      >
        {loading ? (
          <>
            <svg
              className="h-4 w-4 animate-spin"
              xmlns="http://www.w3.org/2000/svg"
              fill="none"
              viewBox="0 0 24 24"
              aria-hidden="true"
            >
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
            </svg>
            Entrando...
          </>
        ) : (
          <>Ver este negocio →</>
        )}
      </div>
    </button>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function DemoPage() {
  const router = useRouter();
  const setAuth = useAuthStore((s) => s.setAuth);
  const [loadingEmail, setLoadingEmail] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleTenantSelect(tenant: DemoTenant) {
    if (loadingEmail) return;
    setError(null);
    setLoadingEmail(tenant.email);
    try {
      const data = await loginRequest({ email: tenant.email, password: tenant.password });
      setAuth(data.access_token, {
        id: data.user.user_id,
        email: data.user.email,
        full_name: data.user.full_name,
        role: data.user.role_code,
        tenant_id: data.user.tenant_id,
      });
      router.replace("/dashboard");
    } catch {
      setError(
        "No se pudo acceder al tenant demo. Verificá que el backend esté corriendo y que hayas ejecutado make seed-demo."
      );
      setLoadingEmail(null);
    }
  }

  return (
    <div className="min-h-screen bg-vk-bg-dark">
      <DemoBanner />

      {/* Nav */}
      <nav className="border-b border-vk-border-dark/60 px-6 py-4">
        <div className="mx-auto flex max-w-5xl items-center justify-between">
          <VektorLogo variant="full" size="md" theme="dark" />
          <a
            href="/register"
            className="rounded-lg bg-vk-blue px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-vk-blue-hover focus:outline-none focus:ring-2 focus:ring-vk-blue/40"
          >
            Crear mi cuenta gratis
          </a>
        </div>
      </nav>

      {/* Content */}
      <main className="mx-auto max-w-5xl px-6 py-16">
        {/* Header */}
        <div className="mb-12 text-center">
          <h1 className="text-3xl font-bold text-vk-text-light sm:text-4xl">
            Elegí un negocio demo
          </h1>
          <p className="mx-auto mt-4 max-w-xl text-base text-vk-text-muted">
            Tres escenarios reales del rubro. Explorá el dashboard sin crear una cuenta.
          </p>
        </div>

        {/* Error */}
        {error && (
          <div className="mb-8 rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-400">
            {error}
          </div>
        )}

        {/* Cards */}
        <div className="grid gap-6 sm:grid-cols-3">
          {DEMO_TENANTS.map((tenant) => (
            <TenantCard
              key={tenant.email}
              tenant={tenant}
              loading={loadingEmail === tenant.email}
              onClick={() => handleTenantSelect(tenant)}
            />
          ))}
        </div>

        {/* Footer CTA */}
        <div className="mt-16 text-center">
          <p className="mb-4 text-sm text-vk-text-muted">
            ¿Listo para usar Véktor con tus propios datos?
          </p>
          <a
            href="/register"
            className="inline-block rounded-xl bg-vk-blue px-8 py-4 text-base font-semibold text-white transition-colors hover:bg-vk-blue-hover focus:outline-none focus:ring-2 focus:ring-vk-blue/40"
          >
            Crear mi cuenta gratis
          </a>
          <p className="mt-3 text-xs text-vk-text-muted">
            3 minutos. Sin datos exactos. Sin tarjeta de crédito.
          </p>
        </div>
      </main>
    </div>
  );
}
