"use client";

import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useAuthStore } from "@/stores/authStore";
import {
  exchangeGoogleSession,
  linkPendingOAuth,
} from "@/services/auth.service";
import type { OAuthLinkRequiredResponse } from "@/types/api";

type Stage = "loading" | "error" | "link_required";

export default function OAuthCallbackPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { token, setAuth } = useAuthStore();

  const [stage, setStage] = useState<Stage>("loading");
  const [errorMsg, setErrorMsg] = useState("");
  const [linkData, setLinkData] = useState<OAuthLinkRequiredResponse | null>(null);
  const [password, setPassword] = useState("");
  const [linkLoading, setLinkLoading] = useState(false);
  const [linkError, setLinkError] = useState("");

  useEffect(() => {
    if (token) {
      router.replace("/chat");
      return;
    }

    const error = searchParams.get("error");
    const sessionId = searchParams.get("session_id");

    if (error) {
      setErrorMsg("Google rechazó el acceso. Intentá de nuevo.");
      setStage("error");
      return;
    }

    if (!sessionId) {
      setErrorMsg("Sesión inválida. Intentá iniciar sesión de nuevo.");
      setStage("error");
      return;
    }

    void (async () => {
      try {
        const result = await exchangeGoogleSession(sessionId);
        if ("status" in result && result.status === "link_required") {
          setLinkData(result as OAuthLinkRequiredResponse);
          setStage("link_required");
        } else {
          const auth = result as {
            access_token: string;
            refresh_token: string;
            user: { user_id: string; email: string; full_name: string; role_code: string; tenant_id: string };
          };
          setAuth(auth.access_token, auth.refresh_token, {
            id: auth.user.user_id,
            email: auth.user.email,
            full_name: auth.user.full_name,
            role: auth.user.role_code,
            tenant_id: auth.user.tenant_id,
          });
          router.replace("/chat");
        }
      } catch {
        setErrorMsg("No se pudo completar el inicio de sesión con Google. Intentá de nuevo.");
        setStage("error");
      }
    })();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function handleLink(e: React.FormEvent) {
    e.preventDefault();
    if (!linkData || !password) return;
    setLinkLoading(true);
    setLinkError("");
    try {
      const auth = await linkPendingOAuth({
        pending_oauth_session_id: linkData.pending_oauth_session_id,
        email: linkData.email,
        password,
      });
      setAuth(auth.access_token, auth.refresh_token, {
        id: auth.user.user_id,
        email: auth.user.email,
        full_name: auth.user.full_name,
        role: auth.user.role_code,
        tenant_id: auth.user.tenant_id,
      });
      router.replace("/chat");
    } catch {
      setLinkError("Contraseña incorrecta. Intentá de nuevo.");
    } finally {
      setLinkLoading(false);
    }
  }

  if (stage === "loading") {
    return (
      <div className="flex min-h-screen items-center justify-center bg-vk-bg-light">
        <div className="flex flex-col items-center gap-4">
          <div className="h-8 w-8 animate-spin rounded-full border-4 border-vk-border-w border-t-vk-blue" />
          <p className="text-sm text-vk-text-muted">Iniciando sesión con Google...</p>
        </div>
      </div>
    );
  }

  if (stage === "error") {
    return (
      <div className="flex min-h-screen items-center justify-center bg-vk-bg-light px-4">
        <div className="w-full max-w-sm rounded-xl border border-vk-border-w bg-vk-surface-w p-8 text-center shadow-vk-md">
          <p className="mb-2 text-base font-semibold text-vk-text-primary">Error de autenticación</p>
          <p className="mb-6 text-sm text-vk-text-muted">{errorMsg}</p>
          <a
            href="/login"
            className="inline-flex items-center justify-center rounded-lg bg-vk-blue px-4 py-2.5 text-sm font-medium text-white hover:bg-vk-blue-hover transition-colors"
          >
            Volver al inicio de sesión
          </a>
        </div>
      </div>
    );
  }

  // link_required
  return (
    <div className="flex min-h-screen items-center justify-center bg-vk-bg-light px-4">
      <div className="w-full max-w-sm rounded-xl border border-vk-border-w bg-vk-surface-w p-8 shadow-vk-md">
        <h1 className="mb-1 text-lg font-semibold text-vk-text-primary">
          Vinculá tu cuenta
        </h1>
        <p className="mb-6 text-sm text-vk-text-muted">
          Ya existe una cuenta con el email{" "}
          <span className="font-medium text-vk-text-secondary">{linkData?.email}</span>.
          Ingresá tu contraseña para vincularla con Google.
        </p>
        <form onSubmit={(e) => void handleLink(e)} className="space-y-4">
          <div>
            <label className="mb-1.5 block text-sm font-medium text-vk-text-secondary">
              Email
            </label>
            <input
              type="email"
              value={linkData?.email ?? ""}
              readOnly
              className="w-full rounded-lg border border-vk-border-w bg-vk-bg-light px-4 py-3 text-sm text-vk-text-muted"
            />
          </div>
          <div>
            <label className="mb-1.5 block text-sm font-medium text-vk-text-secondary">
              Contraseña
            </label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="Tu contraseña de Véktor"
              autoFocus
              className="w-full rounded-lg border border-vk-border-w bg-vk-surface-w px-4 py-3 text-sm text-vk-text-primary placeholder:text-vk-text-placeholder focus:border-vk-blue/40 focus:outline-none focus:ring-2 focus:ring-vk-blue/15"
            />
          </div>
          {linkError && (
            <p className="rounded-lg border border-vk-danger/20 bg-vk-danger-bg px-4 py-3 text-sm text-vk-danger">
              {linkError}
            </p>
          )}
          <button
            type="submit"
            disabled={linkLoading || !password}
            className="flex w-full items-center justify-center gap-2 rounded-lg bg-vk-blue px-4 py-3 text-sm font-semibold text-white hover:bg-vk-blue-hover disabled:opacity-60 transition-colors"
          >
            {linkLoading && (
              <span className="h-4 w-4 animate-spin rounded-full border-2 border-white/40 border-t-white" />
            )}
            {linkLoading ? "Vinculando..." : "Vincular cuenta"}
          </button>
          <p className="text-center text-sm text-vk-text-muted">
            <a href="/login" className="text-vk-blue hover:underline">
              Volver al inicio de sesión
            </a>
          </p>
        </form>
      </div>
    </div>
  );
}
