"use client";

import { useState, useEffect } from "react";
import { useSearchParams } from "next/navigation";
import { resendVerificationRequest } from "@/services/auth.service";

const RESEND_COOLDOWN = 60;

export function VerifyEmailSentPage() {
  const searchParams = useSearchParams();
  const email = searchParams.get("email") ?? "";

  const [resendState, setResendState] = useState<"idle" | "sending" | "sent" | "error">("idle");
  const [countdown, setCountdown] = useState(RESEND_COOLDOWN);

  useEffect(() => {
    if (countdown <= 0) return;
    const id = setTimeout(() => setCountdown((c) => c - 1), 1000);
    return () => clearTimeout(id);
  }, [countdown]);

  // Reset countdown after a successful resend
  async function handleResend() {
    if (!email) return;
    setResendState("sending");
    try {
      await resendVerificationRequest(email);
      setResendState("sent");
      setCountdown(RESEND_COOLDOWN);
    } catch {
      setResendState("error");
    }
  }

  const canResend = countdown <= 0 && resendState !== "sending";

  return (
    <div className="space-y-6 text-center">
      {/* Icon */}
      <div className="flex justify-center">
        <div className="flex h-16 w-16 items-center justify-center rounded-full bg-vk-info-bg">
          <svg className="h-8 w-8 text-vk-blue" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M21.75 6.75v10.5a2.25 2.25 0 01-2.25 2.25h-15a2.25 2.25 0 01-2.25-2.25V6.75m19.5 0A2.25 2.25 0 0019.5 4.5h-15a2.25 2.25 0 00-2.25 2.25m19.5 0v.243a2.25 2.25 0 01-1.07 1.916l-7.5 4.615a2.25 2.25 0 01-2.36 0L3.32 8.91a2.25 2.25 0 01-1.07-1.916V6.75" />
          </svg>
        </div>
      </div>

      {/* Heading */}
      <div>
        <h1 className="text-2xl font-bold text-vk-text-primary">Revisá tu email</h1>
        <p className="mt-2 text-[15px] text-vk-text-muted">
          Te enviamos un link de verificación a{" "}
          {email ? (
            <span className="font-medium text-vk-text-secondary">{email}</span>
          ) : (
            "tu dirección de email"
          )}
          .
        </p>
        <p className="mt-1 text-sm text-vk-text-muted">El link es válido por 24 horas.</p>
      </div>

      {/* Resend section */}
      <div className="rounded-lg border border-vk-border-w bg-vk-bg-light px-5 py-4 text-sm text-vk-text-secondary">
        {resendState === "sent" ? (
          <>
            <p className="font-medium text-vk-success">
              Nuevo link enviado. Revisá tu bandeja de entrada.
            </p>
            {countdown > 0 && (
              <p className="mt-1.5 text-vk-text-muted">
                Podés reenviar en {countdown}s
              </p>
            )}
          </>
        ) : resendState === "error" ? (
          <>
            <p className="text-vk-danger">
              No pudimos reenviar el email. Intentá de nuevo en unos minutos.
            </p>
            {canResend && (
              <button
                type="button"
                onClick={handleResend}
                className="mt-1.5 font-medium text-vk-blue hover:text-vk-blue-hover hover:underline"
              >
                Reintentar
              </button>
            )}
          </>
        ) : countdown > 0 ? (
          <p>
            ¿No lo recibiste? Revisá tu carpeta de spam.{" "}
            <span className="text-vk-text-muted">Podés pedir otro en {countdown}s.</span>
          </p>
        ) : (
          <>
            <p>¿No lo recibiste? Revisá tu carpeta de spam o</p>
            <button
              type="button"
              onClick={handleResend}
              disabled={!canResend || !email}
              className="mt-1.5 font-medium text-vk-blue hover:text-vk-blue-hover hover:underline disabled:cursor-not-allowed disabled:opacity-50"
            >
              {resendState === "sending" ? "Enviando..." : "Reenviar email de verificación"}
            </button>
          </>
        )}
      </div>

      {/* Back to login */}
      <p className="text-sm text-vk-text-muted">
        <a href="/login" className="font-medium text-vk-blue hover:text-vk-blue-hover hover:underline">
          Volver al inicio de sesión
        </a>
      </p>
    </div>
  );
}
