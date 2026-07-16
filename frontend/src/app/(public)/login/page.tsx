import { Suspense } from "react";
import { LoginForm } from "@/features/auth/LoginForm";
import { DoodleCollage } from "@/components/public/DoodleCollage";
import { VektorLogo } from "@/components/ui/VektorLogo";

const CHECK_ITEMS = [
  "Salud financiera en tiempo real",
  "Diseño personalizado para tu negocio",
  "Automatiza todas esas tareas aburridas, simple y claro",
];

export default function LoginPage() {
  return (
    <main className="min-h-screen md:grid md:grid-cols-2">
      {/* Left panel — desktop only */}
      <div className="hidden md:flex flex-col bg-vk-bg-dark px-12 py-12">
        <div>
          <VektorLogo variant="full" size="lg" theme="dark" />
          <p className="mt-3 text-base text-vk-text-muted">
            Trabaja menos y toma las mejores decisiones.
          </p>

          <ul className="mt-10 space-y-4">
            {CHECK_ITEMS.map((item) => (
              <li key={item} className="flex items-center gap-3">
                <span
                  className="flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-full bg-vk-blue/20 text-vk-blue text-xs font-bold"
                  aria-hidden="true"
                >
                  ✓
                </span>
                <span className="text-sm text-vk-text-light">{item}</span>
              </li>
            ))}
          </ul>
        </div>

        {/* Doodles — mismo collage que el hero de la landing. Decorativo: en
            pantallas bajas se recorta antes que empujar la banda de confianza. */}
        <div className="flex min-h-0 flex-1 items-center justify-center overflow-hidden">
          <DoodleCollage className="aspect-square w-full max-w-[380px]" />
        </div>

        {/* Trust band */}
        <div className="border-t border-vk-border-dark pt-6">
          <p className="text-xs font-medium text-vk-text-light">
            Tus datos permanecen bajo tu control
          </p>
          <p className="mt-1 text-xs text-vk-text-muted">
            Sin contabilidad obligatoria. Para negocios en Argentina
          </p>
        </div>
      </div>

      {/* Right panel — form */}
      <div className="flex min-h-screen items-center justify-center bg-vk-surface-w px-8 py-12 md:min-h-0">
        <div className="w-full max-w-[400px]">
          {/* Mobile logo */}
          <div className="mb-8 md:hidden">
            <VektorLogo variant="full" size="md" theme="light" />
          </div>

          <h1 className="mb-2 text-2xl font-semibold text-vk-navy">
            Iniciá sesión
          </h1>
          <p className="mb-8 text-sm text-vk-text-secondary">
            Bienvenido de vuelta a tu salud financiera.
          </p>

          <Suspense>
            <LoginForm />
          </Suspense>
        </div>
      </div>
    </main>
  );
}
