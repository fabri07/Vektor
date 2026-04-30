"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useMemo, useState } from "react";
import { resetPasswordRequest } from "@/services/auth.service";
import { VektorLogo } from "@/components/ui/VektorLogo";

function ResetPasswordForm() {
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
      router.replace("/login?reset=1");
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
          Nueva contraseña
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
          Confirmar contraseña
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
        <p role="alert" className="rounded-lg border border-vk-danger/20 bg-vk-danger-bg px-4 py-3 text-sm text-vk-danger">
          El link es inválido o expiró. Pedí uno nuevo.
        </p>
      )}

      <button
        type="submit"
        disabled={!!error || !password || !confirm || status === "saving"}
        className="flex w-full items-center justify-center rounded-lg bg-vk-blue px-4 py-3 text-[15px] font-semibold text-white transition-colors hover:bg-vk-blue-hover focus:outline-none focus:ring-2 focus:ring-vk-blue/40 disabled:cursor-not-allowed disabled:opacity-60"
      >
        {status === "saving" ? "Guardando..." : "Restablecer contraseña"}
      </button>
    </form>
  );
}

export default function ResetPasswordPage() {
  return (
    <main className="min-h-screen md:grid md:grid-cols-2">
      <div className="hidden flex-col bg-vk-bg-dark px-12 py-12 md:flex">
        <div className="flex-1">
          <VektorLogo variant="full" size="lg" theme="dark" />
          <p className="mt-3 text-base text-vk-text-muted">
            Elegí una contraseña nueva y segura.
          </p>
        </div>
      </div>

      <div className="flex min-h-screen items-center justify-center bg-vk-surface-w px-8 py-12 md:min-h-0">
        <div className="w-full max-w-[400px]">
          <div className="mb-8 md:hidden">
            <VektorLogo variant="full" size="md" theme="light" />
          </div>

          <h1 className="mb-2 text-2xl font-semibold text-vk-navy">
            Nueva contraseña
          </h1>
          <p className="mb-8 text-sm text-vk-text-secondary">
            Usá al menos 8 caracteres, con una letra y un número.
          </p>

          <Suspense>
            <ResetPasswordForm />
          </Suspense>

          <p className="mt-5 text-center text-sm text-vk-text-secondary">
            <Link href="/login" className="font-medium text-vk-blue hover:text-vk-blue-hover">
              Volver a iniciar sesión
            </Link>
          </p>
        </div>
      </div>
    </main>
  );
}
