/**
 * Layout del route group (marketing) — páginas públicas de contenido
 * (/rubros, /quienes-somos, /precios, /privacidad, /tutoriales, /contacto,
 * /terminos). Comparte el nav y el footer públicos.
 *
 * IMPORTANTE: las páginas de auth (login/register/…) viven en (public) y NO
 * heredan este layout, para conservar su experiencia full-screen.
 */

import type { ReactNode } from "react";
import { PublicNav } from "@/components/public/PublicNav";
import { PublicFooter } from "@/components/public/PublicFooter";

export default function MarketingLayout({ children }: { children: ReactNode }) {
  return (
    <div className="min-h-screen bg-vektor-night">
      <PublicNav />
      <main className="pt-16">{children}</main>
      <PublicFooter />
    </div>
  );
}
