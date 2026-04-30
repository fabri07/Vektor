"use client";

import Link from "next/link";
import { useState } from "react";
import { forgotPasswordRequest } from "@/services/auth.service";
import { VektorLogo } from "@/components/ui/VektorLogo";

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [status, setStatus] = useState<"idle" | "sending" | "sent">("idle");

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!email || status === "sending") return;
    setStatus("sending");
    try {
      await forgotPasswordRequest(email);
    } finally {
      setStatus("sent");
    }
  }

  return (
    <main className="min-h-screen md:grid md:grid-cols-2">
      <div className="hidden flex-col bg-vk-bg-dark px-12 py-12 md:flex">
        <div className="flex-1">
          <VektorLogo variant="full" size="lg" theme="dark" />
          <p className="mt-3 text-base text-vk-text-muted">
            Recuperá el acceso a tu negocio.
          </p>
        </div>
      </div>

      <div className="flex min-h-screen items-center justify-center bg-vk-surface-w px-8 py-12 md:min-h-0">
        <div className="w-full max-w-[400px]">
          <div className="mb-8 md:hidden">
            <VektorLogo variant="full" size="md" theme="light" />
          </div>

          <h1 className="mb-2 text-2xl font-semibold text-vk-navy">
            Recuperar contraseña
          </h1>
          <p className="mb-8 text-sm text-vk-text-secondary">
            Ingresá tu email y te enviaremos instrucciones para restablecerla.
          </p>

          <form onSubmit={handleSubmit} className="space-y-5">
            <div>
              <label
                htmlFor="forgot-email"
                className="mb-1.5 block text-sm font-medium text-vk-text-secondary"
              >
                Email
              </label>
              <input
                id="forgot-email"
                type="email"
                autoComplete="email"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                placeholder="tu@email.com"
                className="w-full rounded-lg border border-vk-border-w bg-vk-surface-w px-4 py-3 text-[15px] text-vk-text-primary transition-colors placeholder:text-vk-text-placeholder focus:border-vk-blue/40 focus:outline-none focus:ring-[3px] focus:ring-vk-blue/15"
              />
            </div>

            {status === "sent" && (
              <p role="status" className="rounded-lg border border-vk-success/20 bg-vk-success-bg px-4 py-3 text-sm text-vk-success">
                Si el email existe, recibirás instrucciones en minutos.
              </p>
            )}

            <button
              type="submit"
              disabled={status === "sending" || !email}
              className="flex w-full items-center justify-center rounded-lg bg-vk-blue px-4 py-3 text-[15px] font-semibold text-white transition-colors hover:bg-vk-blue-hover focus:outline-none focus:ring-2 focus:ring-vk-blue/40 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {status === "sending" ? "Enviando..." : "Enviar link"}
            </button>

            <p className="text-center text-sm text-vk-text-secondary">
              <Link href="/login" className="font-medium text-vk-blue hover:text-vk-blue-hover">
                Volver a iniciar sesión
              </Link>
            </p>
          </form>
        </div>
      </div>
    </main>
  );
}
