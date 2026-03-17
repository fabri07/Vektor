"use client";

import { useState, useMemo } from "react";
import { useRouter } from "next/navigation";
import { useRegister } from "@/hooks/useAuth";
import { registerSchema } from "@/validation/auth";
import type { RegisterInput } from "@/validation/auth";
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

const VERTICALS = [
  { value: "kiosco", label: "Kiosco / Almacén" },
  { value: "decoracion_hogar", label: "Decoración del hogar" },
  { value: "limpieza", label: "Productos de limpieza" },
] as const;

const inputClass =
  "w-full rounded-lg border border-[#E5E9F0] px-4 py-3 text-[15px] text-gray-900 placeholder:text-gray-400 focus:border-[#2B7FD4] focus:outline-none focus:ring-[3px] focus:ring-[#2B7FD4]/15 transition-colors";
const inputErrorClass =
  "w-full rounded-lg border border-red-400 px-4 py-3 text-[15px] text-gray-900 placeholder:text-gray-400 focus:border-red-500 focus:outline-none focus:ring-[3px] focus:ring-red-400/15 transition-colors";

type TouchedFields = Record<keyof RegisterInput, boolean>;

const initialTouched: TouchedFields = {
  email: false,
  password: false,
  full_name: false,
  business_name: false,
  vertical_code: false,
};

export function RegisterForm() {
  const router = useRouter();
  const register = useRegister();

  const [values, setValues] = useState<RegisterInput>({
    email: "",
    password: "",
    full_name: "",
    business_name: "",
    vertical_code: "kiosco",
  });
  const [touched, setTouched] = useState<TouchedFields>(initialTouched);
  const [showPassword, setShowPassword] = useState(false);

  const fieldErrors = useMemo(() => {
    const result = registerSchema.safeParse(values);
    if (result.success) return {} as Partial<Record<keyof RegisterInput, string>>;
    return Object.fromEntries(
      result.error.errors.map((e) => [e.path[0], e.message])
    ) as Partial<Record<keyof RegisterInput, string>>;
  }, [values]);

  const serverError = register.error
    ? ((register.error as AxiosError<ApiError>).response?.data?.detail ??
        "No se pudo crear la cuenta. Intentá de nuevo.")
    : null;

  function handleChange(field: keyof RegisterInput, value: string) {
    setValues((prev) => ({ ...prev, [field]: value }));
  }

  function handleBlur(field: keyof RegisterInput) {
    setTouched((prev) => ({ ...prev, [field]: true }));
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setTouched({
      email: true,
      password: true,
      full_name: true,
      business_name: true,
      vertical_code: true,
    });

    const result = registerSchema.safeParse(values);
    if (!result.success) return;

    register.mutate(result.data, {
      onSuccess: () => router.replace("/onboarding"),
    });
  }

  const isValid = registerSchema.safeParse(values).success;

  return (
    <form onSubmit={handleSubmit} noValidate className="space-y-4">
      {/* Full name */}
      <div>
        <label htmlFor="reg-fullname" className="mb-1.5 block text-sm font-medium text-gray-700">
          Nombre completo
        </label>
        <input
          id="reg-fullname"
          type="text"
          autoComplete="name"
          value={values.full_name}
          onChange={(e) => handleChange("full_name", e.target.value)}
          onBlur={() => handleBlur("full_name")}
          placeholder="María García"
          className={touched.full_name && fieldErrors.full_name ? inputErrorClass : inputClass}
          aria-describedby={touched.full_name && fieldErrors.full_name ? "reg-fullname-error" : undefined}
          aria-invalid={touched.full_name && !!fieldErrors.full_name}
        />
        {touched.full_name && fieldErrors.full_name && (
          <p id="reg-fullname-error" role="alert" className="mt-1 text-sm text-red-600">
            {fieldErrors.full_name}
          </p>
        )}
      </div>

      {/* Business name */}
      <div>
        <label htmlFor="reg-business" className="mb-1.5 block text-sm font-medium text-gray-700">
          Nombre del negocio
        </label>
        <input
          id="reg-business"
          type="text"
          autoComplete="organization"
          value={values.business_name}
          onChange={(e) => handleChange("business_name", e.target.value)}
          onBlur={() => handleBlur("business_name")}
          placeholder="Kiosco San Martín"
          className={touched.business_name && fieldErrors.business_name ? inputErrorClass : inputClass}
          aria-describedby={touched.business_name && fieldErrors.business_name ? "reg-business-error" : undefined}
          aria-invalid={touched.business_name && !!fieldErrors.business_name}
        />
        {touched.business_name && fieldErrors.business_name && (
          <p id="reg-business-error" role="alert" className="mt-1 text-sm text-red-600">
            {fieldErrors.business_name}
          </p>
        )}
      </div>

      {/* Vertical */}
      <div>
        <label htmlFor="reg-vertical" className="mb-1.5 block text-sm font-medium text-gray-700">
          Rubro del negocio
        </label>
        <select
          id="reg-vertical"
          value={values.vertical_code}
          onChange={(e) => handleChange("vertical_code", e.target.value)}
          onBlur={() => handleBlur("vertical_code")}
          className={touched.vertical_code && fieldErrors.vertical_code ? inputErrorClass : inputClass}
          aria-describedby={touched.vertical_code && fieldErrors.vertical_code ? "reg-vertical-error" : undefined}
          aria-invalid={touched.vertical_code && !!fieldErrors.vertical_code}
        >
          {VERTICALS.map((v) => (
            <option key={v.value} value={v.value}>
              {v.label}
            </option>
          ))}
        </select>
        {touched.vertical_code && fieldErrors.vertical_code && (
          <p id="reg-vertical-error" role="alert" className="mt-1 text-sm text-red-600">
            {fieldErrors.vertical_code}
          </p>
        )}
      </div>

      {/* Email */}
      <div>
        <label htmlFor="reg-email" className="mb-1.5 block text-sm font-medium text-gray-700">
          Email
        </label>
        <input
          id="reg-email"
          type="email"
          autoComplete="email"
          value={values.email}
          onChange={(e) => handleChange("email", e.target.value)}
          onBlur={() => handleBlur("email")}
          placeholder="tu@email.com"
          className={touched.email && fieldErrors.email ? inputErrorClass : inputClass}
          aria-describedby={touched.email && fieldErrors.email ? "reg-email-error" : undefined}
          aria-invalid={touched.email && !!fieldErrors.email}
        />
        {touched.email && fieldErrors.email && (
          <p id="reg-email-error" role="alert" className="mt-1 text-sm text-red-600">
            {fieldErrors.email}
          </p>
        )}
      </div>

      {/* Password */}
      <div>
        <label htmlFor="reg-password" className="mb-1.5 block text-sm font-medium text-gray-700">
          Contraseña
        </label>
        <div className="relative">
          <input
            id="reg-password"
            type={showPassword ? "text" : "password"}
            autoComplete="new-password"
            value={values.password}
            onChange={(e) => handleChange("password", e.target.value)}
            onBlur={() => handleBlur("password")}
            placeholder="Mínimo 8 caracteres"
            className={`${touched.password && fieldErrors.password ? inputErrorClass : inputClass} pr-11`}
            aria-describedby={touched.password && fieldErrors.password ? "reg-password-error" : undefined}
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
          <p id="reg-password-error" role="alert" className="mt-1 text-sm text-red-600">
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
        disabled={register.isPending || !isValid}
        className="flex w-full items-center justify-center gap-2 rounded-lg bg-[#2B7FD4] px-4 py-3 text-[15px] font-semibold text-white transition-colors hover:bg-[#1E6BB8] focus:outline-none focus:ring-2 focus:ring-[#2B7FD4]/40 disabled:cursor-not-allowed disabled:opacity-60"
      >
        {register.isPending && (
          <span className="h-4 w-4 animate-spin rounded-full border-2 border-white/40 border-t-white" />
        )}
        {register.isPending ? "Creando cuenta..." : "Empezar gratis"}
      </button>

      <p className="text-center text-sm text-gray-500">
        ¿Ya tenés cuenta?{" "}
        <a href="/login" className="font-medium text-[#2B7FD4] hover:text-[#1E6BB8] focus:outline-none focus:underline">
          Iniciá sesión
        </a>
      </p>
    </form>
  );
}
