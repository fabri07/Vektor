"use client";

import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { getMeRequest } from "@/services/auth.service";
import { useAuthStore } from "@/stores/authStore";

/**
 * Valida la sesión contra `/auth/me` al montar y cada vez que la ventana vuelve
 * a tener foco (B5).
 *
 * El rol guardado en `authStore` puede estar viejo: el dueño pudo convertir a
 * este usuario en cajero, o en la misma PC pudo entrar otra persona. Confiar en
 * lo persistido mostraría la navegación del dueño —y los datos ya cacheados— a
 * quien no le corresponde. Si cambió el usuario o el rol, se VACÍA la caché de
 * TanStack Query antes de volver a mostrar nada.
 *
 * Estados: `pending` hasta la primera respuesta; `ok` validado; `offline` si la
 * primera validación falló por red (se sigue con el rol guardado: la caja tiene
 * que poder abrir sin internet). Un 401 lo resuelve el interceptor de `api`.
 */
export function useSessionSync(): "pending" | "ok" | "offline" {
  const queryClient = useQueryClient();
  const [estado, setEstado] = useState<"pending" | "ok" | "offline">("pending");

  useEffect(() => {
    let vivo = true;

    async function validar(): Promise<void> {
      try {
        const me = await getMeRequest();
        if (!vivo) return;
        const antes = useAuthStore.getState().user;
        if (!antes || antes.id !== me.user_id || antes.role !== me.role_code) {
          queryClient.clear();
        }
        useAuthStore.getState().updateUser({
          id: me.user_id,
          email: me.email,
          full_name: me.full_name,
          role: me.role_code,
          tenant_id: me.tenant_id,
          phone: me.phone,
          pos_permissions: me.pos_permissions ?? [],
        });
        setEstado("ok");
      } catch {
        if (vivo) setEstado((e) => (e === "pending" ? "offline" : e));
      }
    }

    void validar();
    window.addEventListener("focus", validar);
    return () => {
      vivo = false;
      window.removeEventListener("focus", validar);
    };
  }, [queryClient]);

  return estado;
}
