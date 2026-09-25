"use client";

import { Button } from "@/components/ui/Button";
import { logoutRequest } from "@/services/auth.service";
import { useAuthStore } from "@/stores/authStore";

/**
 * Lo que ve un usuario de caja hasta que exista la pantalla de caja (B13).
 *
 * Sin esto entraría a la app del dueño: la barra lateral, el chat y cada página
 * responderían 403, porque el backend le deniega todo lo que no es caja.
 */
export function CashierHome() {
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
    <div className="flex h-screen items-center justify-center bg-vk-bg-light p-6">
      <div className="max-w-md rounded-xl border border-vk-border-w bg-vk-surface-w p-6 text-center">
        <p className="text-xs uppercase tracking-wide text-vk-text-muted">Usuario de caja</p>
        <h1 className="mt-2 text-lg font-semibold text-vk-text-primary">
          Hola{user?.full_name ? `, ${user.full_name}` : ""}
        </h1>
        <p className="mt-3 text-sm text-vk-text-secondary">
          Tu usuario es de caja. La pantalla de caja todavía no está disponible: el dueño del
          negocio te va a avisar cuando puedas empezar a cobrar desde acá.
        </p>
        <Button className="mt-5" variant="secondary" onClick={() => void salir()}>
          Cerrar sesión
        </Button>
      </div>
    </div>
  );
}
