"use client";

/**
 * `/definir-password?token=` — el usuario cuya solicitud se aprobó define su
 * primera contraseña.
 *
 * Clon de `(public)/reset-password/page.tsx` con otro copy: no es un "olvidé mi
 * contraseña", es el estreno de la cuenta. El mecanismo es el MISMO
 * (`resetPasswordRequest` contra el token de invitación que mandó el mail de
 * aprobación) — un endpoint paralelo solo duplicaría reglas de contraseña.
 *
 * Éxito → `/login?password_set=1`.
 */

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useMemo, useState } from "react";

import { VektorLogo } from "@/components/ui/VektorLogo";
import { resetPasswordRequest } from "@/services/auth.service";

function DefinirPasswordForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const token = searchParams.get("token") ?? "";
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [status, setStatus] = useState<"idle" | "saving" | "error">("idle");

  const error = useMemo(() => {
    if (!token) return "El link no contiene un token válido.";
    if (password && password.length < 8) return "La contraseña debe tener al menos 8 caracteres.";
    if (password && !/[A-Za-z]/.test(password)) return "La contraseña debe incluir una letra.";
    if (password && !/\d/.test(password)) return "La contraseña debe incluir un número.";
    if (confirm && password !== confirm) return "Las contraseñas no coinciden.";
    return null;
  }, [confirm, password, token]);

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (error || !password || status === "saving") return;
    setStatus("saving");
    try {
      await resetPasswordRequest(token, password);
      router.replace("/login?password_set=1");
    } catch {
      setStatus("error");
    }
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-5">
      <div>
        <label
          htmlFor="new-password"
          className="mb-1.5 block text-sm font-medium text-vk-text-secondary"
        >
          Tu contraseña
        </label>
        <input
          id="new-password"
          type="password"
          autoComplete="new-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          className="w-full rounded-lg border border-vk-border-w bg-vk-surface-w px-4 py-3 text-[15px] text-vk-text-primary transition-colors focus:border-vk-blue/40 focus:outline-none focus:ring-[3px] focus:ring-vk-blue/15"
        />
      </div>

      <div>
        <label
          htmlFor="confirm-password"
          className="mb-1.5 block text-sm font-medium text-vk-text-secondary"
        >
          Repetila
        </label>
        <input
          id="confirm-password"
          type="password"
          autoComplete="new-password"
          value={confirm}
          onChange={(event) => setConfirm(event.target.value)}
          className="w-full rounded-lg border border-vk-border-w bg-vk-surface-w px-4 py-3 text-[15px] text-vk-text-primary transition-colors focus:border-vk-blue/40 focus:outline-none focus:ring-[3px] focus:ring-vk-blue/15"
        />
      </div>

      {error && <p className="text-sm text-vk-danger">{error}</p>}
      {status === "error" && (
        <p
          role="alert"
          className="rounded-lg border border-vk-danger/20 bg-vk-danger-bg px-4 py-3 text-sm text-vk-danger"
        >
          El link es inválido o venció. Escribinos respondiendo el mail de aprobación y te
          mandamos uno nuevo.
        </p>
      )}

      <button
        type="submit"
        disabled={!!error || !password || !confirm || status === "saving"}
        className="flex w-full items-center justify-center rounded-lg bg-vk-blue px-4 py-3 text-[15px] font-semibold text-white transition-colors hover:bg-vk-blue-hover focus:outline-none focus:ring-2 focus:ring-vk-blue/40 disabled:cursor-not-allowed disabled:opacity-60"
      >
        {status === "saving" ? "Guardando..." : "Crear mi contraseña"}
      </button>
    </form>
  );
}

export default function DefinirPasswordPage() {
  return (
    <main className="min-h-screen md:grid md:grid-cols-2">
      <div className="hidden flex-col bg-vk-bg-dark px-12 py-12 md:flex">
        <div className="flex-1">
          <VektorLogo variant="full" size="lg" theme="dark" />
          <p className="mt-3 text-base text-vk-text-muted">
            Tu solicitud fue aprobada. Bienvenido.
          </p>
        </div>
      </div>

      <div className="flex min-h-screen items-center justify-center bg-vk-surface-w px-8 py-12 md:min-h-0">
        <div className="w-full max-w-[400px]">
          <div className="mb-8 md:hidden">
            <VektorLogo variant="full" size="md" theme="light" />
          </div>

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
        </div>
      </div>
    </main>
  );
}
