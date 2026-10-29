"use client";

import { useAccountStatusStore } from "@/stores/accountStatusStore";
import { useAuthStore } from "@/stores/authStore";

const MENSAJE_SUSPENDIDA =
  "Tu cuenta está suspendida. Escribinos para reactivarla: los datos están a salvo.";
const MENSAJE_SOLO_LECTURA =
  "Tu cuenta está en modo solo lectura: podés consultar tus datos, pero no cargar ni modificar nada hasta que se reactive.";

/**
 * Aviso de cuenta bloqueada por el soporte (panel SUPERADMIN, F1).
 *
 * - Suspendida: pantalla completa, salvo en la caja (`enCaja`), donde queda como
 *   franja arriba para que el cajero siga viendo su cola de ventas pendientes —que
 *   no se borra— mientras espera la reactivación.
 * - Solo lectura: franja. La app se puede mirar, no escribir.
 */
export function AccountBlockNotice({ enCaja = false }: { enCaja?: boolean }) {
  const block = useAccountStatusStore((s) => s.block);
  const message = useAccountStatusStore((s) => s.message);
  const logout = useAuthStore((s) => s.logout);

  if (!block) return null;

  const texto = message ?? (block === "SUSPENDED" ? MENSAJE_SUSPENDIDA : MENSAJE_SOLO_LECTURA);

  if (block === "SUSPENDED" && !enCaja) {
    return (
      <div
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="cuenta-bloqueada-titulo"
        className="fixed inset-0 z-[100] flex items-center justify-center bg-vk-bg-light p-6"
      >
        <div className="max-w-md text-center">
          <h1 id="cuenta-bloqueada-titulo" className="mb-3 text-xl font-semibold text-vk-text-primary">
            Cuenta suspendida
          </h1>
          <p className="mb-6 text-sm text-vk-text-muted">{texto}</p>
          <button
            type="button"
            onClick={() => logout()}
            className="rounded-md border border-vk-border-w px-4 py-2 text-sm font-medium text-vk-text-secondary hover:bg-vk-surface-w"
          >
            Cerrar sesión
          </button>
        </div>
      </div>
    );
  }

  return (
    <div
      role="status"
      className="w-full bg-vk-danger/10 px-4 py-2 text-center text-sm text-vk-danger"
    >
      {texto}
    </div>
  );
}
