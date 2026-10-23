"use client";

import { Button } from "@/components/ui/Button";
import { logoutRequest } from "@/services/auth.service";
import { useAuthStore } from "@/stores/authStore";

/**
 * Lo que ve un cajero en una PC que no es caja (B6/B13).
 *
 * El servidor le rechaza toda escritura sin una terminal habilitada
 * (`TERMINAL_REQUIRED`), así que dejarlo armar un carrito sería hacerle perder
 * el trabajo al cobrar. Se le dice antes, con qué hacer.
 */
export function CashierHome({ reason }: { reason: "no_terminal" | "terminal_invalid" }) {
  const user = useAuthStore((s) => s.user);
  const logout = useAuthStore((s) => s.logout);

  async function salir(): Promise<void> {
    try {
      await logoutRequest();
    } catch {
      // Cerrar la sesión local igual: el token vence solo.
    }
    logout();
  }

  return (
    <div className="flex h-screen items-center justify-center bg-pos-bg p-6">
      <div className="max-w-md rounded-xl border border-pos-border bg-pos-surface p-6 text-center">
        <p className="text-xs uppercase tracking-wide text-pos-muted">Usuario de caja</p>
        <h1 className="mt-2 text-lg font-semibold text-pos-text">
          Hola{user?.full_name ? `, ${user.full_name}` : ""}
        </h1>
        <p className="mt-3 text-sm text-pos-text-2">
          {reason === "terminal_invalid"
            ? "Esta PC fue dada de baja como caja, así que desde acá no se puede cobrar."
            : "Esta PC no está habilitada como caja, así que desde acá no se puede cobrar."}{" "}
          Pedile al dueño del negocio que la habilite desde esta PC en Configuración → Seguridad → Cajas habilitadas (o desde el link de la caja).
        </p>
        <Button className="mt-5" variant="secondary" onClick={() => void salir()}>
          Cerrar sesión
        </Button>
      </div>
    </div>
  );
}
