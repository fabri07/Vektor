import { RegisterForm } from "@/features/auth/RegisterForm";
import { VektorLogo } from "@/components/ui/VektorLogo";

const CHECK_ITEMS = [
  "Salud financiera en tiempo real",
  "Diseño personalizado para tu negocio",
  "Automatiza todas esas tareas aburridas, simple y claro",
];

const TRUST_ITEMS = [
  "Tus datos permanecen bajo tu control",
  "Sin contabilidad obligatoria",
  "Para negocios argentinos",
];

export default function RegisterPage() {
  return (
    <main className="min-h-screen md:grid md:grid-cols-2">
      {/* Left panel — desktop only */}
      <div className="hidden md:flex flex-col bg-vk-bg-dark px-12 py-12">
        <div className="flex-1">
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

        {/* Trust band */}
        <div className="border-t border-vk-border-dark pt-6 space-y-2">
          {TRUST_ITEMS.map((item) => (
            <p key={item} className="text-xs text-vk-text-muted">
              {item}
            </p>
          ))}
        </div>
      </div>

      {/* Right panel — form */}
      <div className="flex min-h-screen items-center justify-center bg-vk-surface-w px-8 py-12 md:min-h-0 md:overflow-y-auto">
        <div className="w-full max-w-[400px]">
          {/* Mobile logo */}
          <div className="mb-8 md:hidden">
            <VektorLogo variant="full" size="md" theme="light" />
          </div>

          <h1 className="mb-2 text-2xl font-semibold text-vk-navy">
            Creá tu cuenta
          </h1>
          <p className="mb-8 text-sm text-vk-text-secondary">
            Empezá gratis. Sin tarjeta de crédito.
          </p>

          <RegisterForm />
        </div>
      </div>
    </main>
  );
}
