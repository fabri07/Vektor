"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAuthStore } from "@/stores/authStore";
import { isSuperadmin } from "@/lib/roles";

/**
 * Guard de UX de la sección de plataforma. NO es la seguridad: cada ruta
 * `/admin/*` del backend exige SUPERADMIN y devuelve 403 a cualquier otro rol.
 * Esto sólo evita mostrarle a un dueño una pantalla que no puede usar.
 */
export default function AdminLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const role = useAuthStore((s) => s.user?.role);
  const permitido = isSuperadmin(role);

  useEffect(() => {
    if (role !== undefined && !permitido) {
      router.replace("/dashboard");
    }
  }, [role, permitido, router]);

  if (!permitido) return null;
  return <>{children}</>;
}
