import { Suspense } from "react";
import type { Metadata } from "next";
import Link from "next/link";

import { FunnelSplitShell } from "@/components/public/FunnelSplitShell";
import { DefinirPasswordForm } from "./DefinirPasswordContenido";

/**
 * `/definir-password?token=` — el usuario cuya solicitud se aprobó define su
 * primera contraseña. Server Component por la `metadata`; el formulario y su
 * estado viven en `DefinirPasswordContenido`.
 */
export const metadata: Metadata = {
  title: "Definí tu contraseña | Véktor",
  description: "Tu solicitud fue aprobada. Elegí una contraseña para entrar.",
};

export default function DefinirPasswordPage() {
  return (
    <FunnelSplitShell aside="Tu solicitud fue aprobada. Bienvenido.">
      <h1 className="mb-2 text-2xl font-semibold text-vk-navy">
        Definí tu contraseña
      </h1>
      <p className="mb-8 text-sm text-vk-text-secondary">
        Es la última puerta antes de entrar. Usá al menos 8 caracteres, con una letra
        y un número.
      </p>

      <Suspense>
        <DefinirPasswordForm />
      </Suspense>

      <p className="mt-5 text-center text-sm text-vk-text-secondary">
        <Link href="/login" className="font-medium text-vk-blue hover:text-vk-blue-hover">
          Ir a iniciar sesión
        </Link>
      </p>
    </FunnelSplitShell>
  );
}
