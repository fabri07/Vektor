"use client";

import { useState, useMemo } from "react";
import { useRouter } from "next/navigation";
import { useLogin } from "@/hooks/useAuth";
import { loginSchema } from "@/validation/auth";
import type { LoginInput } from "@/validation/auth";
import type { AxiosError } from "axios";
import type { ApiError } from "@/types/api";

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
  "w-full rounded-lg border border-[#E5E9F0] px-4 py-3 text-[15px] text-gray-900 placeholder:text-gray-400 focus:border-[#2B7FD4] focus:outline-none focus:ring-[3px] focus:ring-[#2B7FD4]/15 transition-colors";
const inputErrorClass =
  "w-full rounded-lg border border-red-400 px-4 py-3 text-[15px] text-gray-900 placeholder:text-gray-400 focus:border-red-500 focus:outline-none focus:ring-[3px] focus:ring-red-400/15 transition-colors";

export function LoginForm() {
  const router = useRouter();
  const login = useLogin();

  const [values, setValues] = useState<LoginInput>({ email: "", password: "" });
  const [touched, setTouched] = useState({ email: false, password: false });
  const [showPassword, setShowPassword] = useState(false);

  const fieldErrors = useMemo(() => {
    const result = loginSchema.safeParse(values);
    if (result.success) return {} as Partial<Record<keyof LoginInput, string>>;
    return Object.fromEntries(
      result.error.errors.map((e) => [e.path[0], e.message])
    ) as Partial<Record<keyof LoginInput, string>>;
  }, [values]);

  const serverError = login.error
    ? ((login.error as AxiosError<ApiError>).response?.data?.detail ??
        "Credenciales incorrectas. Intentá de nuevo.")
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

    const result = loginSchema.safeParse(values);
    if (!result.success) return;

    login.mutate(result.data, {
      onSuccess: () => router.replace("/dashboard"),
    });
  }

  const isValid = loginSchema.safeParse(values).success;

  return (
    <form onSubmit={handleSubmit} noValidate className="space-y-5">
      {/* Email */}
      <div>
        <label htmlFor="login-email" className="mb-1.5 block text-sm font-medium text-gray-700">
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
          <p id="login-email-error" role="alert" className="mt-1 text-sm text-red-600">
            {fieldErrors.email}
          </p>
        )}
      </div>

      {/* Password */}
      <div>
        <label htmlFor="login-password" className="mb-1.5 block text-sm font-medium text-gray-700">
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
            className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600 focus:outline-none focus:ring-2 focus:ring-[#2B7FD4]/30 rounded"
            aria-label={showPassword ? "Ocultar contraseña" : "Mostrar contraseña"}
          >
            {showPassword ? <EyeOff /> : <EyeOpen />}
          </button>
        </div>
        {touched.password && fieldErrors.password && (
          <p id="login-password-error" role="alert" className="mt-1 text-sm text-red-600">
            {fieldErrors.password}
          </p>
        )}
      </div>

      {/* Server error */}
      {serverError && (
        <p role="alert" className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-600">
          {serverError}
        </p>
      )}

      {/* Submit */}
      <button
        type="submit"
        disabled={login.isPending || !isValid}
        className="flex w-full items-center justify-center gap-2 rounded-lg bg-[#2B7FD4] px-4 py-3 text-[15px] font-semibold text-white transition-colors hover:bg-[#1E6BB8] focus:outline-none focus:ring-2 focus:ring-[#2B7FD4]/40 disabled:cursor-not-allowed disabled:opacity-60"
      >
        {login.isPending && (
          <span className="h-4 w-4 animate-spin rounded-full border-2 border-white/40 border-t-white" />
        )}
        {login.isPending ? "Ingresando..." : "Iniciar sesión"}
      </button>

      <p className="text-center text-sm text-gray-500">
        ¿No tenés cuenta?{" "}
        <a href="/register" className="font-medium text-[#2B7FD4] hover:text-[#1E6BB8] focus:outline-none focus:underline">
          Creá una gratis
        </a>
      </p>
    </form>
  );
}
