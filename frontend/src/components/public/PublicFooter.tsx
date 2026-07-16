/**
 * PublicFooter — footer de marketing en tema dark.
 *
 * Estructura pedida (espeja adhoc): links con "Política de privacidad" al final,
 * "Tutoriales", y fila de iconos sociales.
 *
 * Iconos sociales: mientras no haya URL real, se renderizan DESHABILITADOS
 * (span, no <a href="#">) para no generar ruido de accesibilidad ni navegación
 * confusa. Cuando el usuario pase las URLs reales, se completan en SOCIALS.
 */

import Link from "next/link";
import { VektorLogo } from "@/components/ui/VektorLogo";

const FOOTER_LINKS = [
  { label: "Tutoriales", href: "/tutoriales" },
  { label: "Términos", href: "/terminos" },
  { label: "Contacto", href: "/contacto" },
  { label: "Política de privacidad", href: "/privacidad" }, // siempre al final
];

// href: null ⇒ ícono deshabilitado hasta tener la URL real de la red.
const SOCIALS: { name: string; href: string | null; icon: JSX.Element }[] = [
  {
    name: "Instagram",
    href: null,
    icon: (
      <path d="M12 2.2c3.2 0 3.6 0 4.85.07 1.17.05 1.8.25 2.23.41.56.22.96.48 1.38.9.42.42.68.82.9 1.38.16.42.36 1.06.41 2.23.06 1.27.07 1.65.07 4.85s0 3.58-.07 4.85c-.05 1.17-.25 1.8-.41 2.23-.22.56-.48.96-.9 1.38-.42.42-.82.68-1.38.9-.42.16-1.06.36-2.23.41-1.27.06-1.65.07-4.85.07s-3.58 0-4.85-.07c-1.17-.05-1.8-.25-2.23-.41a3.7 3.7 0 0 1-1.38-.9 3.7 3.7 0 0 1-.9-1.38c-.16-.42-.36-1.06-.41-2.23C2.21 15.58 2.2 15.2 2.2 12s0-3.58.07-4.85c.05-1.17.25-1.8.41-2.23.22-.56.48-.96.9-1.38.42-.42.82-.68 1.38-.9.42-.16 1.06-.36 2.23-.41C8.42 2.21 8.8 2.2 12 2.2Zm0 3.2A6.6 6.6 0 1 0 12 18.6 6.6 6.6 0 0 0 12 5.4Zm0 10.9a4.3 4.3 0 1 1 0-8.6 4.3 4.3 0 0 1 0 8.6Zm6.85-11.15a1.54 1.54 0 1 1-3.08 0 1.54 1.54 0 0 1 3.08 0Z" />
    ),
  },
  {
    name: "LinkedIn",
    href: null,
    icon: (
      <path d="M4.98 3.5a2.5 2.5 0 1 1 0 5 2.5 2.5 0 0 1 0-5ZM3 9h4v12H3V9Zm7 0h3.8v1.64h.05c.53-.95 1.83-1.95 3.76-1.95 4.02 0 4.76 2.5 4.76 5.76V21h-4v-5.3c0-1.27-.02-2.9-1.77-2.9-1.77 0-2.04 1.38-2.04 2.8V21h-4V9Z" />
    ),
  },
  {
    name: "YouTube",
    href: null,
    icon: (
      <path d="M23.5 6.5a3 3 0 0 0-2.11-2.12C19.5 3.9 12 3.9 12 3.9s-7.5 0-9.39.48A3 3 0 0 0 .5 6.5 31 31 0 0 0 0 12a31 31 0 0 0 .5 5.5 3 3 0 0 0 2.11 2.12C4.5 20.1 12 20.1 12 20.1s7.5 0 9.39-.48A3 3 0 0 0 23.5 17.5 31 31 0 0 0 24 12a31 31 0 0 0-.5-5.5ZM9.6 15.5v-7l6.2 3.5-6.2 3.5Z" />
    ),
  },
  {
    name: "WhatsApp",
    href: null,
    icon: (
      <path d="M.5 23.5l1.65-6a11.4 11.4 0 1 1 4.35 4.3L.5 23.5ZM6.8 19.9l.37.22a9.5 9.5 0 1 0-3.28-3.3l.24.38-.98 3.56 3.65-.86ZM17.5 14.4c-.24-.12-1.4-.7-1.62-.78-.22-.08-.38-.12-.54.12-.16.24-.62.78-.76.94-.14.16-.28.18-.52.06-.24-.12-1-.37-1.9-1.18-.7-.63-1.18-1.4-1.32-1.64-.14-.24-.01-.37.1-.49.11-.11.24-.28.36-.42.12-.14.16-.24.24-.4.08-.16.04-.3-.02-.42-.06-.12-.54-1.3-.74-1.78-.2-.47-.4-.4-.54-.41h-.46c-.16 0-.42.06-.64.3-.22.24-.84.82-.84 2s.86 2.32.98 2.48c.12.16 1.7 2.6 4.12 3.64.58.25 1.02.4 1.37.5.58.19 1.1.16 1.52.1.46-.07 1.4-.57 1.6-1.12.2-.55.2-1.02.14-1.12-.06-.1-.22-.16-.46-.28Z" />
    ),
  },
];

export function PublicFooter() {
  return (
    <footer className="border-t border-vektor-border bg-vektor-night py-12">
      <div className="mx-auto flex max-w-7xl flex-col items-center gap-8 px-6">
        <div className="flex flex-col items-center gap-3">
          <VektorLogo variant="full" size="md" theme="dark" />
          <p className="max-w-md text-center text-sm text-vektor-muted">
            Véktor te ayuda a decidir mejor sin vivir en planillas.
          </p>
        </div>

        <nav className="flex flex-wrap items-center justify-center gap-x-2 gap-y-2 text-sm">
          {FOOTER_LINKS.map((link, i) => (
            <span key={link.href} className="flex items-center gap-2">
              <Link href={link.href} className="text-vektor-muted hover:text-vektor-white">
                {link.label}
              </Link>
              {i < FOOTER_LINKS.length - 1 && (
                <span aria-hidden className="text-vektor-border">
                  •
                </span>
              )}
            </span>
          ))}
        </nav>

        <div className="flex items-center gap-3">
          {SOCIALS.map((s) =>
            s.href ? (
              <a
                key={s.name}
                href={s.href}
                target="_blank"
                rel="noopener noreferrer"
                aria-label={s.name}
                className="flex h-10 w-10 items-center justify-center rounded-full border border-vektor-border text-vektor-muted transition hover:border-vektor-blue hover:text-vektor-white"
              >
                <svg viewBox="0 0 24 24" className="h-5 w-5" fill="currentColor" aria-hidden>
                  {s.icon}
                </svg>
              </a>
            ) : (
              <span
                key={s.name}
                aria-hidden
                title={`${s.name} (próximamente)`}
                className="flex h-10 w-10 cursor-default items-center justify-center rounded-full border border-vektor-border/50 text-vektor-border"
              >
                <svg viewBox="0 0 24 24" className="h-5 w-5" fill="currentColor">
                  {s.icon}
                </svg>
              </span>
            )
          )}
        </div>

        <p className="text-xs text-vektor-muted">
          © {new Date().getFullYear()} Véktor. Hecho en Argentina.
        </p>
      </div>
    </footer>
  );
}
