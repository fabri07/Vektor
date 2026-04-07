"use client";

import { useState, useMemo } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useLogin } from "@/hooks/useAuth";
import { resendVerificationRequest, getGoogleOAuthUrl } from "@/services/auth.service";
import { loginSchema } from "@/validation/auth";
import type { LoginInput } from "@/validation/auth";
import type { AxiosError } from "axios";
import type { ApiError } from "@/types/api";

const GoogleIcon = () => (
  <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true">
    <path d="M17.64 9.2c0-.637-.057-1.251-.164-1.84H9v3.481h4.844c-.209 1.125-.843 2.078-1.796 2.717v2.258h2.908c1.702-1.567 2.684-3.875 2.684-6.615Z" fill="#4285F4"/>
    <path d="M9 18c2.43 0 4.467-.806 5.956-2.18l-2.908-2.259c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332A8.997 8.997 0 0 0 9 18Z" fill="#34A853"/>
    <path d="M3.964 10.71A5.41 5.41 0 0 1 3.682 9c0-.593.102-1.17.282-1.71V4.958H.957A8.996 8.996 0 0 0 0 9c0 1.452.348 2.827.957 4.042l3.007-2.332Z" fill="#FBBC05"/>
    <path d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0A8.997 8.997 0 0 0 .957 4.958L3.964 6.29C4.672 4.163 6.656 3.58 9 3.58Z" fill="#EA4335"/>
  </svg>
);

const EyeOpen = () => (
  <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
    <circle cx="12" cy="12" r="3" />
  </svg>
);

const EyeOff = () => (
  <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" />
    <line x1="1" y1="1" x2="23" y2="23" />
  </svg>
);

const inputClass =
  "w-full rounded-lg border border-vk-border-w px-4 py-3 text-[15px] text-vk-text-primary placeholder:text-vk-text-placeholder focus:border-vk-blue/40 focus:outline-none focus:ring-[3px] focus:ring-vk-blue/15 transition-colors bg-vk-surface-w";
const inputErrorClass =
  "w-full rounded-lg border border-vk-danger/60 px-4 py-3 text-[15px] text-vk-text-primary placeholder:text-vk-text-placeholder focus:border-vk-danger/60 focus:outline-none focus:ring-[3px] focus:ring-vk-danger/20 transition-colors bg-vk-surface-w";

export function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const justRegistered = searchParams.get("registered") === "1";
  const login = useLogin();

  const [values, setValues] = useState<LoginInput>({ email: "", password: "" });
  const [touched, setTouched] = useState({ email: false, password: false });
  const [showPassword, setShowPassword] = useState(false);
  const [resendState, setResendState] = useState<"idle" | "sending" | "sent">("idle");
  const [googleLoading, setGoogleLoading] = useState(false);
  const [googleError, setGoogleError] = useState<string | null>(null);

  const fieldErrors = useMemo(() => {
    const result = loginSchema.safeParse(values);
    if (result.success) return {} as Partial<Record<keyof LoginInput, string>>;
    return Object.fromEntries(
      result.error.errors.map((e) => [e.path[0], e.message])
    ) as Partial<Record<keyof LoginInput, string>>;
  }, [values]);

  const loginError = login.error as AxiosError<ApiError> | null;
  const isEmailNotVerified =
    loginError?.response?.status === 403 &&
    loginError?.response?.data?.detail === "email_not_verified";

  const serverError =
    !isEmailNotVerified && login.error
      ? (() => {
          const detail = loginError?.response?.data?.detail;
          if (!detail) return "Credenciales incorrectas. Intentá de nuevo.";
          if (typeof detail === "string") return detail;
          return detail.map((e) => e.msg).join(" · ");
        })()
      : null;

  function handleChange(field: keyof LoginInput, value: string) {
    setValues((prev) => ({ ...prev, [field]: value }));
  }

  function handleBlur(field: keyof LoginInput) {
    setTouched((prev) => ({ ...prev, [field]: true }));
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setTouched({ email: true, password: true });
    setResendState("idle");

    const result = loginSchema.safeParse(values);
    if (!result.success) return;

    login.mutate(result.data, {
      onSuccess: () => router.replace("/chat"),
    });
  }

  async function handleGoogleLogin() {
    setGoogleLoading(true);
    setGoogleError(null);
    try {
      const { authorization_url } = await getGoogleOAuthUrl();
      window.location.href = authorization_url;
    } catch {
      setGoogleError("No se pudo iniciar el acceso con Google. Intentá de nuevo.");
      setGoogleLoading(false);
    }
  }

  async function handleResend() {
    if (!values.email) return;
    setResendState("sending");
    try {
      await resendVerificationRequest(values.email);
      setResendState("sent");
    } catch {
      setResendState("idle");
    }
  }

  const isValid = loginSchema.safeParse(values).success;

  return (
    <form onSubmit={handleSubmit} noValidate className="space-y-5">
      {/* Email */}
      <div>
        <label htmlFor="login-email" className="mb-1.5 block text-sm font-medium text-vk-text-secondary">
          Email
        </label>
        <input
          id="login-email"
          type="email"
          autoComplete="email"
          value={values.email}
          onChange={(e) => handleChange("email", e.target.value)}
          onBlur={() => handleBlur("email")}
          placeholder="tu@email.com"
          className={touched.email && fieldErrors.email ? inputErrorClass : inputClass}
          aria-describedby={touched.email && fieldErrors.email ? "login-email-error" : undefined}
          aria-invalid={touched.email && !!fieldErrors.email}
        />
        {touched.email && fieldErrors.email && (
          <p id="login-email-error" role="alert" className="mt-1 text-sm text-vk-danger">
            {fieldErrors.email}
          </p>
        )}
      </div>

      {/* Password */}
      <div>
        <label htmlFor="login-password" className="mb-1.5 block text-sm font-medium text-vk-text-secondary">
          Contraseña
        </label>
        <div className="relative">
          <input
            id="login-password"
            type={showPassword ? "text" : "password"}
            autoComplete="current-password"
            value={values.password}
            onChange={(e) => handleChange("password", e.target.value)}
            onBlur={() => handleBlur("password")}
            placeholder="Mínimo 8 caracteres"
            className={`${touched.password && fieldErrors.password ? inputErrorClass : inputClass} pr-11`}
            aria-describedby={touched.password && fieldErrors.password ? "login-password-error" : undefined}
            aria-invalid={touched.password && !!fieldErrors.password}
          />
          <button
            type="button"
            onClick={() => setShowPassword((v) => !v)}
            className="absolute right-3 top-1/2 -translate-y-1/2 text-vk-text-muted hover:text-vk-text-secondary focus:outline-none focus:ring-2 focus:ring-vk-blue/30 rounded"
            aria-label={showPassword ? "Ocultar contraseña" : "Mostrar contraseña"}
          >
            {showPassword ? <EyeOff /> : <EyeOpen />}
          </button>
        </div>
        {touched.password && fieldErrors.password && (
          <p id="login-password-error" role="alert" className="mt-1 text-sm text-vk-danger">
            {fieldErrors.password}
          </p>
        )}
      </div>

      {/* Email not verified banner */}
      {isEmailNotVerified && (
        <div role="alert" className="rounded-lg border border-vk-warning/30 bg-vk-warning-bg px-4 py-3 text-sm text-vk-warning">
          <p className="font-medium">Verificá tu email para poder ingresar.</p>
          <p className="mt-0.5 text-vk-warning/80">
            Revisá tu bandeja de entrada o reenviá el link de verificación.
          </p>
          {resendState === "sent" ? (
            <p className="mt-2 font-medium text-vk-success">Email enviado. Revisá tu bandeja.</p>
          ) : (
            <button
              type="button"
              onClick={handleResend}
              disabled={resendState === "sending" || !values.email}
              className="mt-2 text-vk-blue font-medium hover:underline disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {resendState === "sending" ? "Enviando..." : "Reenviar verificación"}
            </button>
          )}
        </div>
      )}

      {/* Account just created (no email verification required) */}
      {justRegistered && (
        <p role="status" className="rounded-lg border border-vk-success/20 bg-vk-success-bg px-4 py-3 text-sm text-vk-success">
          ¡Cuenta creada! Ingresá con tu email y contraseña.
        </p>
      )}

      {/* Generic server error */}
      {serverError && (
        <p role="alert" className="rounded-lg border border-vk-danger/20 bg-vk-danger-bg px-4 py-3 text-sm text-vk-danger">
          {serverError}
        </p>
      )}

      {googleError && (
        <p role="alert" className="rounded-lg border border-vk-danger/20 bg-vk-danger-bg px-4 py-3 text-sm text-vk-danger">
          {googleError}
        </p>
      )}

      {/* Submit */}
      <button
        type="submit"
        disabled={login.isPending || !isValid}
        className="flex w-full items-center justify-center gap-2 rounded-lg bg-vk-blue px-4 py-3 text-[15px] font-semibold text-white transition-colors hover:bg-vk-blue-hover focus:outline-none focus:ring-2 focus:ring-vk-blue/40 disabled:cursor-not-allowed disabled:opacity-60"
      >
        {login.isPending && (
          <span className="h-4 w-4 animate-spin rounded-full border-2 border-white/40 border-t-white" />
        )}
        {login.isPending ? "Ingresando..." : "Iniciar sesión"}
      </button>

      {/* Divider */}
      <div className="relative">
        <div className="absolute inset-0 flex items-center">
          <div className="w-full border-t border-vk-border-w" />
        </div>
        <div className="relative flex justify-center text-xs uppercase">
          <span className="bg-vk-surface-w px-2 text-vk-text-muted">o</span>
        </div>
      </div>

      {/* Google OAuth */}
      <button
        type="button"
        onClick={() => void handleGoogleLogin()}
        disabled={googleLoading}
        className="flex w-full items-center justify-center gap-2.5 rounded-lg border border-vk-border-w bg-vk-surface-w px-4 py-3 text-[15px] font-medium text-vk-text-primary transition-colors hover:bg-vk-bg-light focus:outline-none focus:ring-2 focus:ring-vk-blue/30 disabled:cursor-not-allowed disabled:opacity-60"
      >
        {googleLoading ? (
          <span className="h-4 w-4 animate-spin rounded-full border-2 border-vk-border-w border-t-vk-text-muted" />
        ) : (
          <GoogleIcon />
        )}
        {googleLoading ? "Redirigiendo..." : "Continuar con Google"}
      </button>

      <p className="text-center text-sm text-vk-text-secondary">
        ¿No tenés cuenta?{" "}
        <a href="/register" className="font-medium text-vk-blue hover:text-vk-blue-hover focus:outline-none focus:underline">
          Creá una gratis
        </a>
      </p>
    </form>
  );
}
