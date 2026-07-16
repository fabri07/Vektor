"use client";

import { useEffect, useRef, useState } from "react";
import Image from "next/image";
import { useRouter } from "next/navigation";
import {
  Activity,
  Boxes,
  ChevronLeft,
  ChevronRight,
  Truck,
} from "lucide-react";
import { useAuthStore } from "@/stores/authStore";
import { PublicNav } from "@/components/public/PublicNav";
import { PublicFooter } from "@/components/public/PublicFooter";
import { DoodleHero } from "@/components/public/DoodleHero";

const SOCIAL_PROOF_CARDS = [
  { src: "/screenshots/kiosco.jpg", rubro: "Kiosco" },
  { src: "/screenshots/verduleria.jpg", rubro: "Verdulería" },
  { src: "/screenshots/peluqueria.jpg", rubro: "Peluquería" },
  { src: "/screenshots/gimnasio.jpg", rubro: "Gimnasio" },
  { src: "/screenshots/taller-mecanico.jpg", rubro: "Taller mecánico" },
  { src: "/screenshots/tienda-de-decoracion.jpg", rubro: "Tienda de decoración" },
  {
    src: "/screenshots/venta-de-articulos-de-limpieza.jpg",
    rubro: "Venta de artículos de limpieza",
  },
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

function SocialProofStrip() {
  const cards = [...SOCIAL_PROOF_CARDS, ...SOCIAL_PROOF_CARDS];

  return (
    <section className="overflow-hidden border-y border-vektor-border bg-vektor-night py-8">
      <div className="group flex min-w-max animate-marquee gap-5 px-6 hover:[animation-play-state:paused]">
        {cards.map((card, index) => (
          <article
            key={`${card.src}-${index}`}
            className="group/card relative h-[220px] w-[300px] overflow-hidden rounded-2xl border border-vektor-border bg-vektor-ink shadow-lg"
          >
            <Image
              src={card.src}
              alt={`Véktor para ${card.rubro}`}
              fill
              sizes="300px"
              className="object-cover transition-all duration-300 group-hover/card:scale-[1.03]"
            />
            <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-vektor-night via-vektor-night/70 to-transparent px-4 pb-4 pt-10">
              <p className="text-sm font-medium text-white">{card.rubro}</p>
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
          <p className="text-xs font-semibold uppercase tracking-[0.22em] text-vektor-teal">
            Qué resuelve Véktor
          </p>
          <h2 className="mt-4 font-display text-4xl font-bold uppercase leading-tight tracking-tight text-vektor-white">
            Un tablero pensado para dueños de negocio, no para técnicos.
          </h2>
        </div>

        <div className="grid gap-6 md:grid-cols-3">
          {FEATURE_HIGHLIGHTS.map((feature) => {
            const Icon = feature.icon;
            return (
              <article key={feature.title} className="vektor-card p-7">
                <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-vektor-surface text-vektor-teal">
                  <Icon className="h-6 w-6" />
                </div>
                <h3 className="mt-6 font-display text-2xl font-bold uppercase tracking-tight text-vektor-white">
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

const SCREENSHOTS = [
  {
    src: "/screenshots/dashboard-kiosco.png",
    caption: "Salud del negocio: score, caja, margen y stock de un vistazo.",
  },
  {
    src: "/screenshots/dashboard-limpieza.png",
    caption: "El riesgo principal y la próxima acción concreta, siempre a la vista.",
  },
  {
    src: "/screenshots/productos-kiosco.png",
    caption: "Inventario con estado de stock y valor a precio de costo.",
  },
  {
    src: "/screenshots/productos-deco.png",
    caption: "Catálogo por categoría, adaptado a tu rubro.",
  },
  {
    src: "/screenshots/gastos-kiosco.png",
    caption: "Gastos categorizados y clasificados entre operativos y mercadería.",
  },
  {
    src: "/screenshots/ventas-kiosco.png",
    caption: "Ventas del período con ticket promedio y comparativo mensual.",
  },
];

function ScreenshotCarousel() {
  const trackRef = useRef<HTMLDivElement>(null);
  const rafRef = useRef<number | null>(null);
  const [active, setActive] = useState(0);

  useEffect(() => {
    return () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    };
  }, []);

  function goTo(index: number) {
    const track = trackRef.current;
    if (!track) return;
    const clamped = Math.max(0, Math.min(index, SCREENSHOTS.length - 1));
    const slide = track.children[clamped] as HTMLElement | undefined;
    const reduceMotion =
      typeof window !== "undefined" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    slide?.scrollIntoView({
      behavior: reduceMotion ? "auto" : "smooth",
      inline: "center",
      block: "nearest",
    });
    setActive(clamped);
  }

  // Throttle scroll work to one layout read per animation frame.
  function handleScroll() {
    if (rafRef.current !== null) return;
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = null;
      const track = trackRef.current;
      if (!track) return;
      const center = track.scrollLeft + track.clientWidth / 2;
      let closest = 0;
      let min = Infinity;
      Array.from(track.children).forEach((child, i) => {
        const el = child as HTMLElement;
        const c = el.offsetLeft + el.clientWidth / 2;
        const distance = Math.abs(c - center);
        if (distance < min) {
          min = distance;
          closest = i;
        }
      });
      setActive(closest);
    });
  }

  return (
    <div className="mt-8">
      <div className="relative">
        <div
          ref={trackRef}
          onScroll={handleScroll}
          role="group"
          aria-roledescription="Carrusel"
          aria-label="Capturas de pantalla de Véktor"
          className="flex snap-x snap-mandatory gap-4 overflow-x-auto pb-1 [-ms-overflow-style:none] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
        >
          {SCREENSHOTS.map((shot, i) => (
            <figure key={shot.src} className="w-full shrink-0 snap-center">
              <div className="overflow-hidden rounded-[20px] border border-vektor-border bg-vektor-ink shadow-lg">
                <Image
                  src={shot.src}
                  alt={shot.caption}
                  width={2880}
                  height={1800}
                  className="h-auto w-full"
                  sizes="(max-width: 1280px) 100vw, 1200px"
                  priority={i === 0}
                />
              </div>
              <figcaption className="mt-4 text-center text-sm leading-6 text-vektor-muted">
                {shot.caption}
              </figcaption>
            </figure>
          ))}
        </div>

        <button
          type="button"
          onClick={() => goTo(active - 1)}
          disabled={active === 0}
          aria-label="Imagen anterior"
          className="absolute left-3 top-[46%] flex h-10 w-10 -translate-y-1/2 items-center justify-center rounded-full border border-vektor-border bg-vektor-night/80 text-vektor-white backdrop-blur-sm transition hover:border-vektor-blue hover:text-vektor-blue disabled:pointer-events-none disabled:opacity-0"
        >
          <ChevronLeft className="h-5 w-5" />
        </button>
        <button
          type="button"
          onClick={() => goTo(active + 1)}
          disabled={active === SCREENSHOTS.length - 1}
          aria-label="Imagen siguiente"
          className="absolute right-3 top-[46%] flex h-10 w-10 -translate-y-1/2 items-center justify-center rounded-full border border-vektor-border bg-vektor-night/80 text-vektor-white backdrop-blur-sm transition hover:border-vektor-blue hover:text-vektor-blue disabled:pointer-events-none disabled:opacity-0"
        >
          <ChevronRight className="h-5 w-5" />
        </button>
      </div>

      <div className="mt-6 flex items-center justify-center gap-2">
        {SCREENSHOTS.map((shot, i) => (
          <button
            key={shot.src}
            type="button"
            onClick={() => goTo(i)}
            aria-label={`Ir a la imagen ${i + 1}`}
            aria-current={active === i}
            className={`h-2 rounded-full transition-all ${
              active === i ? "w-6 bg-vektor-blue" : "w-2 bg-vektor-border hover:bg-vektor-muted"
            }`}
          />
        ))}
      </div>
    </div>
  );
}

function WorkflowPreview() {
  return (
    <section className="bg-vektor-night pb-28">
      <div className="mx-auto max-w-7xl px-6">
        <div className="vektor-card overflow-hidden p-8">
          <div className="flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
            <div>
              <p className="text-xs font-semibold uppercase tracking-[0.22em] text-vektor-blue">
                Vista previa
              </p>
              <h2 className="mt-3 font-display text-4xl font-bold uppercase tracking-tight text-vektor-white">
                Todo lo importante en una sola pantalla.
              </h2>
            </div>
            <p className="max-w-xl text-sm leading-7 text-vektor-muted">
              Caja, margen, stock, proveedores y salud general del negocio en una experiencia oscura, clara y accionable.
            </p>
          </div>

          <ScreenshotCarousel />
        </div>
      </div>
    </section>
  );
}

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
      <PublicNav />
      <DoodleHero />
      <SocialProofStrip />
      <FeatureHighlights />
      <WorkflowPreview />
      <PublicFooter />
    </>
  );
}
