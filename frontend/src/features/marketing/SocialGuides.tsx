"use client";

import { useState, type ComponentType } from "react";
import {
  Briefcase,
  Building2,
  Camera,
  ChevronDown,
  Download,
  ExternalLink,
  MapPin,
  MessageCircle,
  PlayCircle,
  Users,
  Video,
} from "lucide-react";

// Etapa 1: accesos genéricos a cada plataforma (abren la web de la app, no el
// perfil del usuario todavía). Etapa 2 (follow-up): guardar el usuario/URL de
// cada red para abrir el perfil directo.
//
// reviewedOn: los pasos de exportación de cada red cambian seguido — el sello
// avisa qué tan fresca es la instrucción.
const REVIEWED_ON = "Junio 2026";

// Columnas del CSV que Véktor entiende (espejo de CSV_COLUMNS en la página).
const TEMPLATE_COLUMNS = [
  "platform",
  "metric_date",
  "followers",
  "reach",
  "engagement",
  "ads_spend_ars",
] as const;

interface SocialGuide {
  id: string;
  label: string;
  url: string;
  brandColor: string;
  icon: ComponentType<{ className?: string }>;
  feedsMetrics: boolean;
  download: { desktop: string[]; mobile: string[] };
  note?: string;
}

const SOCIAL_GUIDES: SocialGuide[] = [
  {
    id: "instagram",
    label: "Instagram",
    url: "https://www.instagram.com",
    brandColor: "#E1306C",
    icon: Camera,
    feedsMetrics: true,
    download: {
      desktop: [
        "Entrá a Meta Business Suite (business.facebook.com) con la cuenta vinculada.",
        "Andá a Estadísticas → Resultados y elegí el rango de fechas.",
        "Tocá Exportar y descargá el archivo.",
      ],
      mobile: [
        "Abrí Instagram y tocá tu perfil.",
        "Tocá Estadísticas (Professional dashboard).",
        "Anotá seguidores, alcance e interacciones del período.",
      ],
    },
  },
  {
    id: "facebook",
    label: "Facebook",
    url: "https://www.facebook.com",
    brandColor: "#1877F2",
    icon: Users,
    feedsMetrics: true,
    download: {
      desktop: [
        "Entrá a Meta Business Suite (business.facebook.com).",
        "Estadísticas → elegí la página y el rango de fechas.",
        "Exportá los datos a archivo.",
      ],
      mobile: [
        "Abrí la app de tu página y tocá Estadísticas profesionales.",
        "Anotá seguidores, alcance e interacciones del período.",
      ],
    },
  },
  {
    id: "tiktok",
    label: "TikTok",
    url: "https://www.tiktok.com",
    brandColor: "#FE2C55",
    icon: Video,
    feedsMetrics: true,
    download: {
      desktop: [
        "Entrá a TikTok desde la web con tu cuenta.",
        "Perfil → Ver estadísticas (necesitás cuenta Business).",
        "Descargá los datos del rango de fechas.",
      ],
      mobile: [
        "Perfil → menú (☰) → Herramientas para creadores → Estadísticas.",
        "Anotá seguidores, vistas y likes del período.",
      ],
    },
  },
  {
    id: "meta",
    label: "Meta Business Suite",
    url: "https://business.facebook.com",
    brandColor: "#0668E1",
    icon: Building2,
    feedsMetrics: true,
    download: {
      desktop: [
        "Entrá a business.facebook.com.",
        "Estadísticas → Resultados, elegí el rango de fechas.",
        "Exportá Instagram y Facebook juntos a un archivo.",
      ],
      mobile: [
        "Abrí la app Meta Business Suite.",
        "Estadísticas → anotá los totales del período.",
      ],
    },
  },
  {
    id: "linkedin",
    label: "LinkedIn",
    url: "https://www.linkedin.com",
    brandColor: "#0A66C2",
    icon: Briefcase,
    feedsMetrics: true,
    download: {
      desktop: [
        "Entrá a tu página de empresa en LinkedIn.",
        "Analíticas → Seguidores / Contenido, elegí el rango.",
        "Tocá Exportar para bajar el archivo.",
      ],
      mobile: [
        "Abrí la página de tu empresa en la app.",
        "Tocá Analíticas y anotá seguidores e impresiones del período.",
      ],
    },
  },
  {
    id: "google_business",
    label: "Google Business Profile",
    url: "https://business.google.com",
    brandColor: "#4285F4",
    icon: MapPin,
    feedsMetrics: true,
    download: {
      desktop: [
        "Entrá a business.google.com con la cuenta del negocio.",
        "Rendimiento → elegí el rango de fechas.",
        "Anotá las visualizaciones e interacciones del período.",
      ],
      mobile: [
        "Buscá tu negocio en Google Maps con tu cuenta.",
        "Tocá Promocionar → Rendimiento para ver las métricas.",
      ],
    },
  },
  {
    id: "youtube",
    label: "YouTube",
    url: "https://studio.youtube.com",
    brandColor: "#FF0000",
    icon: PlayCircle,
    feedsMetrics: true,
    download: {
      desktop: [
        "Entrá a YouTube Studio (studio.youtube.com).",
        "Estadísticas → elegí el rango de fechas.",
        "Tocá Exportar (CSV) arriba a la derecha.",
      ],
      mobile: [
        "Abrí la app YouTube Studio.",
        "Estadísticas → anotá suscriptores y vistas del período.",
      ],
    },
  },
  {
    id: "whatsapp",
    label: "WhatsApp Business",
    url: "https://business.whatsapp.com",
    brandColor: "#25D366",
    icon: MessageCircle,
    feedsMetrics: false,
    note: "Por ahora WhatsApp Business no alimenta las métricas de Véktor — usalo como canal de gestión y contacto.",
    download: {
      desktop: [
        "Entrá a business.whatsapp.com para gestionar tu cuenta y catálogo.",
      ],
      mobile: [
        "Abrí WhatsApp Business → Herramientas para la empresa.",
      ],
    },
  },
];

function buildTemplateCsv(): string {
  const header = TEMPLATE_COLUMNS.join(",");
  const example = "instagram,2026-06-01,1200,5400,320,15000";
  return `${header}\n${example}\n`;
}

function downloadTemplate(): void {
  // BOM UTF-8 (﻿) para que Excel es-AR lea bien acentos si se agregan columnas
  // con texto — mismo criterio que SmartTable.exportCSV.
  const blob = new Blob(["﻿" + buildTemplateCsv()], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "vektor-metricas-redes-plantilla.csv";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export function SocialGuides() {
  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="font-display text-base font-semibold text-vk-text-primary">Tus redes</h2>
          <p className="text-sm text-vk-text-secondary">
            Abrí cada red y seguí los pasos para bajar tus estadísticas y cargarlas acá.
          </p>
          <p className="mt-0.5 text-xs text-vk-text-muted">
            Instrucciones revisadas el {REVIEWED_ON}.
          </p>
        </div>
        <button
          type="button"
          onClick={downloadTemplate}
          className="inline-flex items-center gap-1.5 rounded-xl border border-vk-border-w bg-vk-surface-w px-3 py-2 text-sm font-medium text-vk-text-primary transition-colors hover:bg-vk-bg-light"
        >
          <Download className="h-4 w-4" />
          Descargar plantilla CSV
        </button>
      </div>

      <div className="grid gap-3 md:grid-cols-2">
        {SOCIAL_GUIDES.map((g) => (
          <SocialGuideCard key={g.id} guide={g} />
        ))}
      </div>
    </section>
  );
}

function SocialGuideCard({ guide }: { guide: SocialGuide }) {
  const [open, setOpen] = useState(false);
  const Icon = guide.icon;

  return (
    <article
      className="overflow-hidden rounded-2xl border border-vk-border-w bg-vk-surface-w"
      style={{ borderLeft: `3px solid ${guide.brandColor}` }}
    >
      <div className="flex items-center justify-between gap-3 p-4">
        <div className="flex min-w-0 items-center gap-3">
          <span
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl"
            style={{ backgroundColor: `${guide.brandColor}1A`, color: guide.brandColor }}
          >
            <Icon className="h-5 w-5" />
          </span>
          <div className="min-w-0">
            <p className="truncate font-medium text-vk-text-primary">{guide.label}</p>
            {!guide.feedsMetrics ? (
              <p className="text-xs text-vk-text-muted">No alimenta métricas</p>
            ) : null}
          </div>
        </div>
        <a
          href={guide.url}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex shrink-0 items-center gap-1.5 rounded-xl border border-vk-border-w px-3 py-1.5 text-sm font-medium text-vk-text-primary transition-colors hover:bg-vk-bg-light"
        >
          <ExternalLink className="h-4 w-4" />
          Abrir
        </a>
      </div>

      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-2 border-t border-vk-border-w px-4 py-2.5 text-left text-sm font-medium text-vk-text-secondary transition-colors hover:bg-vk-bg-light"
      >
        Cómo traer tus métricas
        <ChevronDown
          className={`h-4 w-4 transition-transform motion-reduce:transition-none ${
            open ? "rotate-180" : ""
          }`}
        />
      </button>

      {open ? (
        <div className="space-y-4 border-t border-vk-border-w px-4 py-3 text-sm">
          {guide.note ? (
            <p className="rounded-lg bg-vk-bg-light px-3 py-2 text-xs text-vk-text-secondary">
              {guide.note}
            </p>
          ) : null}

          <Step
            n={1}
            title={
              guide.feedsMetrics ? "Bajá tus métricas" : "Gestioná tu cuenta"
            }
          >
            <Instructions title="En computadora" steps={guide.download.desktop} />
            <Instructions title="En el celular" steps={guide.download.mobile} />
          </Step>

          {guide.feedsMetrics ? (
            <Step n={2} title="Cargalas en Véktor">
              <ul className="ml-1 list-inside list-disc space-y-1 text-vk-text-secondary">
                <li>
                  Tocá <span className="font-medium text-vk-text-primary">Cargar métrica</span> acá
                  arriba y completá los datos a mano, o
                </li>
                <li>
                  usá <span className="font-medium text-vk-text-primary">Importar CSV</span> con la
                  plantilla descargable (botón de arriba).
                </li>
              </ul>
            </Step>
          ) : null}
        </div>
      ) : null}
    </article>
  );
}

function Step({
  n,
  title,
  children,
}: {
  n: number;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex gap-3">
      <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-vk-blue/10 text-xs font-bold text-vk-blue">
        {n}
      </span>
      <div className="min-w-0 flex-1 space-y-2">
        <p className="font-medium text-vk-text-primary">{title}</p>
        {children}
      </div>
    </div>
  );
}

function Instructions({ title, steps }: { title: string; steps: string[] }) {
  return (
    <div>
      <p className="text-xs font-semibold uppercase tracking-wide text-vk-text-muted">{title}</p>
      <ol className="ml-1 mt-1 list-inside list-decimal space-y-0.5 text-vk-text-secondary">
        {steps.map((s, i) => (
          <li key={i}>{s}</li>
        ))}
      </ol>
    </div>
  );
}
