"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { Settings } from "lucide-react";

const SCREENS = [
  { id: "dashboard", label: "Resumen", href: "/dashboard" },
  { id: "analisis", label: "Analisis", href: "/dashboard/analisis" },
  { id: "balance", label: "Balance", href: "/dashboard/balance" },
] as const;

interface Props {
  active: "dashboard" | "analisis" | "balance";
}

export function DashboardLaunchpadNav({ active }: Props) {
  const router = useRouter();

  function goTo(screen: (typeof SCREENS)[number]["id"]) {
    const target = SCREENS.find((item) => item.id === screen);
    if (target) router.push(target.href);
  }

  return (
    <>
      <div className="hidden items-center justify-between gap-4 sm:flex">
        <div className="inline-flex rounded-full border border-vektor-border bg-vektor-ink p-1">
          {SCREENS.map((screen) => {
            const isActive = screen.id === active;
            return (
              <Link
                key={screen.id}
                href={screen.href}
                className={[
                  "rounded-full px-4 py-2 text-sm font-medium",
                  isActive
                    ? "bg-gradient-to-r from-vektor-blue to-vektor-teal text-vektor-white shadow-glow"
                    : "text-vektor-body hover:bg-vektor-surface",
                ].join(" ")}
              >
                {screen.label}
              </Link>
            );
          })}
        </div>

        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2">
            {SCREENS.map((screen) => {
              const isActive = screen.id === active;
              return (
                <button
                  key={screen.id}
                  type="button"
                  onClick={() => goTo(screen.id)}
                  aria-label={`Ir a ${screen.label}`}
                  className={[
                    "h-2.5 rounded-full transition-all duration-200",
                    isActive
                      ? "w-8 bg-vektor-white"
                      : "w-2.5 bg-vektor-border hover:bg-vektor-muted",
                  ].join(" ")}
                />
              );
            })}
          </div>

          {/* Acceso directo a Configuración */}
          <Link
            href="/settings"
            className="inline-flex items-center gap-1.5 rounded-full border border-vektor-border bg-vektor-ink px-3 py-1.5 text-sm font-medium text-vektor-body transition-colors hover:bg-vektor-surface hover:text-vektor-white"
          >
            <Settings className="h-4 w-4" />
            Configuración
          </Link>
        </div>
      </div>

      <div className="fixed inset-x-4 bottom-4 z-30 sm:hidden">
        <div className="rounded-full border border-vektor-border bg-vektor-ink/95 p-1 shadow-lg backdrop-blur-xl">
          <div className="grid grid-cols-3 gap-1">
            {SCREENS.map((screen) => {
              const isActive = screen.id === active;
              return (
                <Link
                  key={screen.id}
                  href={screen.href}
                  className={[
                    "rounded-full px-4 py-3 text-center text-sm font-medium",
                    isActive
                      ? "bg-gradient-to-r from-vektor-blue to-vektor-teal text-vektor-white"
                      : "text-vektor-body",
                  ].join(" ")}
                >
                  {screen.label}
                </Link>
              );
            })}
          </div>
        </div>
      </div>
    </>
  );
}
