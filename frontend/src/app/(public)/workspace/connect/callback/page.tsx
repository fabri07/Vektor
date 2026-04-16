"use client";

import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useAuthStore } from "@/stores/authStore";
import { useToastStore } from "@/stores/toastStore";
import { workspaceService } from "@/services/workspace.service";

export default function WorkspaceConnectCallbackPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { token } = useAuthStore();
  const addToast = useToastStore((s) => s.add);
  const [errorMsg, setErrorMsg] = useState("");
  const [done, setDone] = useState(false);

  useEffect(() => {
    if (!token) {
      router.replace("/login");
      return;
    }

    const error = searchParams.get("error");
    const exchangeSessionId = searchParams.get("exchange_session_id");

    if (error) {
      setErrorMsg("Google rechazó el acceso. Intentá conectar de nuevo desde Aplicaciones.");
      setDone(true);
      return;
    }

    if (!exchangeSessionId) {
      setErrorMsg("Sesión inválida. Intentá conectar de nuevo desde Aplicaciones.");
      setDone(true);
      return;
    }

    void (async () => {
      try {
        await workspaceService.exchangeSession(exchangeSessionId);
        addToast("Google Workspace conectado correctamente.", "success");
        router.replace("/apps");
      } catch {
        setErrorMsg(
          "No se pudo conectar Google Workspace. Intentá de nuevo desde Aplicaciones.",
        );
        setDone(true);
      }
    })();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  if (!done) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-vk-bg-light">
        <div className="flex flex-col items-center gap-4">
          <div className="h-8 w-8 animate-spin rounded-full border-4 border-vk-border-w border-t-vk-blue" />
          <p className="text-sm text-vk-text-muted">Conectando Google Workspace...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-vk-bg-light px-4">
      <div className="w-full max-w-sm rounded-xl border border-vk-border-w bg-vk-surface-w p-8 text-center shadow-vk-md">
        <p className="mb-2 text-base font-semibold text-vk-text-primary">Error de conexión</p>
        <p className="mb-6 text-sm text-vk-text-muted">{errorMsg}</p>
        <a
          href="/apps"
          className="inline-flex items-center justify-center rounded-lg bg-vk-blue px-4 py-2.5 text-sm font-medium text-white hover:bg-vk-blue-hover transition-colors"
        >
          Ir a Aplicaciones
        </a>
      </div>
    </div>
  );
}
