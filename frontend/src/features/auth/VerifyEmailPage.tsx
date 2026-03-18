"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useVerifyEmail } from "@/hooks/useAuth";
import { resendVerificationRequest } from "@/services/auth.service";

type Phase = "verifying" | "success" | "error";

export function VerifyEmailPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const token = searchParams.get("token") ?? "";

  const verifyEmail = useVerifyEmail();
  const [phase, setPhase] = useState<Phase>("verifying");
  const [resendEmail, setResendEmail] = useState("");
  const [resendState, setResendState] = useState<"idle" | "sending" | "sent" | "error">("idle");
  const calledRef = useRef(false);

  useEffect(() => {
    if (calledRef.current || !token) {
      if (!token) setPhase("error");
      return;
    }
    calledRef.current = true;

    verifyEmail.mutate(token, {
      onSuccess: () => {
        setPhase("success");
        setTimeout(() => router.replace("/onboarding"), 1500);
      },
      onError: () => {
        setPhase("error");
      },
    });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  async function handleResend(e: React.FormEvent) {
    e.preventDefault();
    if (!resendEmail.trim()) return;
    setResendState("sending");
    try {
      await resendVerificationRequest(resendEmail.trim());
      setResendState("sent");
    } catch {
      setResendState("error");
    }
  }

  if (phase === "verifying") {
    return (
      <div className="flex flex-col items-center gap-4 text-center">
        <div className="h-10 w-10 animate-spin rounded-full border-4 border-vk-blue/20 border-t-vk-blue" />
        <p className="text-[15px] text-vk-text-muted">Verificando tu email...</p>
      </div>
    );
  }

  if (phase === "success") {
    return (
      <div className="flex flex-col items-center gap-4 text-center">
        <div className="flex h-16 w-16 items-center justify-center rounded-full bg-vk-success-bg">
          <svg className="h-8 w-8 text-vk-success" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
          </svg>
        </div>
        <div>
          <h1 className="text-2xl font-bold text-vk-text-primary">Email verificado</h1>
          <p className="mt-2 text-[15px] text-vk-text-muted">
            Tu cuenta está activa. Redirigiendo...
          </p>
        </div>
      </div>
    );
  }

  // phase === "error"
  return (
    <div className="space-y-6">
      <div className="flex flex-col items-center gap-4 text-center">
        <div className="flex h-16 w-16 items-center justify-center rounded-full bg-vk-danger-bg">
          <svg className="h-8 w-8 text-vk-danger" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
          </svg>
        </div>
        <div>
          <h1 className="text-2xl font-bold text-vk-text-primary">Link inválido o expirado</h1>
          <p className="mt-2 text-[15px] text-vk-text-muted">
            El link de verificación ya fue usado o venció. Solicitá uno nuevo.
          </p>
        </div>
      </div>

      {/* Resend form */}
      <form onSubmit={handleResend} className="space-y-3">
        {resendState === "sent" ? (
          <p className="rounded-lg border border-vk-success/20 bg-vk-success-bg px-4 py-3 text-sm font-medium text-vk-success text-center">
            Nuevo link enviado. Revisá tu bandeja de entrada.
          </p>
        ) : (
          <>
            <div>
              <label htmlFor="resend-email" className="mb-1.5 block text-sm font-medium text-vk-text-secondary">
                Tu email
              </label>
              <input
                id="resend-email"
                type="email"
                value={resendEmail}
                onChange={(e) => setResendEmail(e.target.value)}
                placeholder="tu@email.com"
                className="w-full rounded-lg border border-vk-border-w px-4 py-3 text-[15px] text-vk-text-primary placeholder:text-vk-text-placeholder focus:border-vk-blue/40 focus:outline-none focus:ring-[3px] focus:ring-vk-blue/15 transition-colors bg-vk-surface-w"
              />
            </div>
            {resendState === "error" && (
              <p className="text-sm text-vk-danger">
                No pudimos enviar el email. Intentá de nuevo.
              </p>
            )}
            <button
              type="submit"
              disabled={resendState === "sending" || !resendEmail.trim()}
              className="flex w-full items-center justify-center gap-2 rounded-lg bg-vk-blue px-4 py-3 text-[15px] font-semibold text-white transition-colors hover:bg-vk-blue-hover disabled:cursor-not-allowed disabled:opacity-60"
            >
              {resendState === "sending" && (
                <span className="h-4 w-4 animate-spin rounded-full border-2 border-white/40 border-t-white" />
              )}
              {resendState === "sending" ? "Enviando..." : "Reenviar link de verificación"}
            </button>
          </>
        )}
      </form>

      <p className="text-center text-sm text-vk-text-secondary">
        <a href="/login" className="font-medium text-vk-blue hover:text-vk-blue-hover hover:underline">
          Volver al inicio de sesión
        </a>
      </p>
    </div>
  );
}
