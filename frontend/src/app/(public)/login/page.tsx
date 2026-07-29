import { Suspense } from "react";
import type { Metadata } from "next";

import { LoginForm } from "@/features/auth/LoginForm";
import { DoodleCollage } from "@/components/public/DoodleCollage";
import { FunnelSplitShell } from "@/components/public/FunnelSplitShell";

export const metadata: Metadata = {
  title: "Iniciar sesión | Véktor",
  description: "Entrá a tu cuenta de Véktor.",
};

const CHECK_ITEMS = [
  "Salud financiera en tiempo real",
  "Diseño personalizado para tu negocio",
  "Automatiza todas esas tareas aburridas, simple y claro",
];

/**
 * Panel izquierdo propio de `/login`.
 *
 * Es la única pantalla del embudo que lo tiene lleno. Las tres de espera
 * (`/solicitud-enviada`, `/solicitud-verificada`, `/definir-password`) usan el
 * mismo shell con el panel corto: llenarlas es una decisión de diseño
 * pendiente, no un fix técnico, y traer esto tal cual sería tomarla de
 * costado.
 */
function PanelDeLogin() {
  return (
    <>
      <ul className="mt-10 space-y-4">
        {CHECK_ITEMS.map((item) => (
          <li key={item} className="flex items-center gap-3">
            <span
              className="flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-full bg-vk-blue/20 text-xs font-bold text-vk-blue"
              aria-hidden="true"
            >
              ✓
            </span>
            <span className="text-sm text-vk-text-light">{item}</span>
          </li>
        ))}
      </ul>

      {/* Doodles — mismo collage que el hero de la landing. Decorativo: en
          pantallas bajas se recorta antes que empujar la banda de confianza. */}
      <div className="flex min-h-0 flex-1 items-center justify-center overflow-hidden">
        <DoodleCollage className="aspect-square w-full max-w-[380px]" />
      </div>

      <div className="border-t border-vk-border-dark pt-6">
        <p className="text-xs font-medium text-vk-text-light">
          Tus datos permanecen bajo tu control
        </p>
        <p className="mt-1 text-xs text-vk-text-muted">
          Sin contabilidad obligatoria. Para negocios en Argentina
        </p>
      </div>
    </>
  );
}

export default function LoginPage() {
  return (
    <FunnelSplitShell
      aside="Trabaja menos y toma las mejores decisiones."
      asideExtra={<PanelDeLogin />}
    >
      <h1 className="mb-2 text-2xl font-semibold text-vk-navy">Iniciá sesión</h1>
      <p className="mb-8 text-sm text-vk-text-secondary">
        Bienvenido de vuelta a tu salud financiera.
      </p>

      <Suspense>
        <LoginForm />
      </Suspense>
    </FunnelSplitShell>
  );
}
