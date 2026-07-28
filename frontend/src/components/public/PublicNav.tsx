"use client";

/**
 * PublicNav — barra de navegación pública (marketing), estilo adhoc en tema dark.
 *
 * Items: Rubros ▾ (dropdown de verticales) · Quiénes somos · Precios · Contactanos
 * Botones: Iniciar sesión (outline → /login) · Contactanos (filled → /contacto)
 *
 * Accesibilidad: "Rubros" es un disclosure de navegación (NO un role="menu",
 * para no prometer semántica de menú que no implementamos). Operable por teclado:
 * ArrowDown abre y enfoca el primer item, ArrowUp/ArrowDown mueven el foco entre
 * items, Escape cierra y devuelve el foco al botón. Cierra con click afuera.
 * Menú hamburguesa en mobile. Se usa desde el layout (marketing) y la landing raíz.
 */

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { ChevronDown, Menu, X } from "lucide-react";
import { VektorLogo } from "@/components/ui/VektorLogo";

const RUBROS = [
  { label: "Kiosco / Almacén", href: "/rubros#kiosco" },
  { label: "Limpieza", href: "/rubros#limpieza" },
  { label: "Decoración del hogar", href: "/rubros#deco" },
];

const NAV_LINKS = [
  { label: "Quiénes somos", href: "/quienes-somos" },
  { label: "Precios", href: "/precios" },
  { label: "Contactanos", href: "/contacto" },
];

export function PublicNav() {
  const [rubrosOpen, setRubrosOpen] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const rubrosRef = useRef<HTMLDivElement>(null);
  const rubrosTriggerRef = useRef<HTMLButtonElement>(null);
  // Cuando se abre por teclado, enfocar el primer item recién montado el panel.
  const focusFirstOnOpen = useRef(false);

  // Cerrar el dropdown al hacer click afuera. (Escape se maneja localmente en el
  // panel para poder devolver el foco al botón.)
  useEffect(() => {
    function onClickOutside(e: MouseEvent) {
      if (rubrosRef.current && !rubrosRef.current.contains(e.target as Node)) {
        setRubrosOpen(false);
      }
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setMobileOpen(false);
    }
    document.addEventListener("mousedown", onClickOutside);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClickOutside);
      document.removeEventListener("keydown", onKey);
    };
  }, []);

  // Enfoca el primer item cuando el panel se abrió por teclado (ya montado).
  useEffect(() => {
    if (rubrosOpen && focusFirstOnOpen.current) {
      focusFirstOnOpen.current = false;
      const items = rubrosRef.current?.querySelectorAll<HTMLAnchorElement>(
        "a[data-rubro-item]"
      );
      items?.[0]?.focus();
    }
  }, [rubrosOpen]);

  function closeRubrosAndRefocus() {
    setRubrosOpen(false);
    rubrosTriggerRef.current?.focus();
  }

  // Mueve el foco entre los items del dropdown con las flechas (roving focus).
  function onPanelKeyDown(e: React.KeyboardEvent<HTMLDivElement>) {
    if (e.key === "Escape") {
      e.preventDefault();
      closeRubrosAndRefocus();
      return;
    }
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const items = Array.from(
      rubrosRef.current?.querySelectorAll<HTMLAnchorElement>("a[data-rubro-item]") ?? []
    );
    if (items.length === 0) return;
    const current = items.indexOf(document.activeElement as HTMLAnchorElement);
    const delta = e.key === "ArrowDown" ? 1 : -1;
    const next = (current + delta + items.length) % items.length;
    items[next]?.focus();
  }

  function onTriggerKeyDown(e: React.KeyboardEvent<HTMLButtonElement>) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      focusFirstOnOpen.current = true;
      setRubrosOpen(true);
    }
  }

  return (
    <nav className="fixed inset-x-0 top-0 z-50 border-b border-white/10 bg-vektor-night/80 backdrop-blur-md">
      <div className="mx-auto flex h-16 max-w-7xl items-center justify-between px-6">
        <Link href="/" aria-label="Véktor — inicio">
          <VektorLogo variant="full" size="md" theme="dark" />
        </Link>

        {/* Links — desktop */}
        <div className="hidden items-center gap-1 lg:flex">
          <div ref={rubrosRef} className="relative">
            <button
              ref={rubrosTriggerRef}
              type="button"
              onClick={() => setRubrosOpen((v) => !v)}
              onKeyDown={onTriggerKeyDown}
              aria-expanded={rubrosOpen}
              className="flex items-center gap-1 rounded-full px-4 py-2 text-sm font-medium text-vektor-body hover:text-vektor-white"
            >
              Rubros
              <ChevronDown
                className={`h-4 w-4 transition-transform ${rubrosOpen ? "rotate-180" : ""}`}
              />
            </button>
            {rubrosOpen && (
              <div
                onKeyDown={onPanelKeyDown}
                className="absolute left-0 top-full mt-2 min-w-[220px] overflow-hidden rounded-2xl border border-vektor-border bg-vektor-ink py-2 shadow-xl"
              >
                {RUBROS.map((r) => (
                  <Link
                    key={r.href}
                    href={r.href}
                    data-rubro-item
                    onClick={() => setRubrosOpen(false)}
                    className="block px-4 py-2 text-sm text-vektor-body hover:bg-vektor-surface hover:text-vektor-white"
                  >
                    {r.label}
                  </Link>
                ))}
              </div>
            )}
          </div>

          {NAV_LINKS.map((link) => (
            <Link
              key={link.href}
              href={link.href}
              className="rounded-full px-4 py-2 text-sm font-medium text-vektor-body hover:text-vektor-white"
            >
              {link.label}
            </Link>
          ))}
        </div>

        {/* CTAs — desktop */}
        <div className="hidden items-center gap-3 lg:flex">
          <Link
            href="/login"
            className="inline-flex rounded-full border border-white/20 px-4 py-2 text-sm font-medium text-vektor-body hover:border-white/40 hover:text-vektor-white"
          >
            Iniciar sesión
          </Link>
          <Link
            href="/solicitar-acceso?src=nav_desktop"
            className="inline-flex rounded-full bg-gradient-to-r from-vektor-blue to-vektor-teal px-5 py-2.5 text-sm font-semibold text-white hover:opacity-90"
          >
            Pedir acceso
          </Link>
        </div>

        {/* Toggle mobile */}
        <button
          type="button"
          onClick={() => setMobileOpen((v) => !v)}
          aria-label={mobileOpen ? "Cerrar menú" : "Abrir menú"}
          aria-expanded={mobileOpen}
          className="inline-flex h-10 w-10 items-center justify-center rounded-full border border-white/15 text-vektor-body lg:hidden"
        >
          {mobileOpen ? <X className="h-5 w-5" /> : <Menu className="h-5 w-5" />}
        </button>
      </div>

      {/* Menú mobile */}
      {mobileOpen && (
        <div className="border-t border-white/10 bg-vektor-night px-6 py-4 lg:hidden">
          <p className="px-1 pb-1 text-xs font-semibold uppercase tracking-widest text-vektor-muted">
            Rubros
          </p>
          {RUBROS.map((r) => (
            <Link
              key={r.href}
              href={r.href}
              onClick={() => setMobileOpen(false)}
              className="block rounded-lg px-1 py-2 text-sm text-vektor-body hover:text-vektor-white"
            >
              {r.label}
            </Link>
          ))}
          <div className="my-3 h-px bg-white/10" />
          {NAV_LINKS.map((link) => (
            <Link
              key={link.href}
              href={link.href}
              onClick={() => setMobileOpen(false)}
              className="block rounded-lg px-1 py-2 text-sm font-medium text-vektor-body hover:text-vektor-white"
            >
              {link.label}
            </Link>
          ))}
          <div className="mt-4 flex flex-col gap-3">
            <Link
              href="/login"
              onClick={() => setMobileOpen(false)}
              className="inline-flex justify-center rounded-full border border-white/20 px-4 py-2.5 text-sm font-medium text-vektor-body"
            >
              Iniciar sesión
            </Link>
            <Link
              href="/solicitar-acceso?src=nav_mobile"
              onClick={() => setMobileOpen(false)}
              className="inline-flex justify-center rounded-full bg-gradient-to-r from-vektor-blue to-vektor-teal px-5 py-2.5 text-sm font-semibold text-white"
            >
              Pedir acceso
            </Link>
          </div>
        </div>
      )}
    </nav>
  );
}
