"use client";

/**
 * `/solicitud-verificada?token=` — confirma el email de una solicitud.
 *
 * Postea el token al montar (una sola vez, guardado con un ref), igual que
 * `features/auth/VerifyEmailPage.tsx`. Es POST y no GET a propósito: los
 * escáneres de correo corporativos y los prefetchers de links hacen GET y
 * consumirían el token antes que el usuario.
 *
 * Declara un tiempo de respuesta esperado, porque acá termina la parte del
 * usuario: a partir de ahora espera. Lo que NO se expone es nada del criterio
 * ni del orden de la cola — que una solicitud Premium se revise antes es
 * información interna.
 */

import { Suspense, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";

import { VektorLogo } from "@/components/ui/VektorLogo";
import { useVerifyAccessRequest } from "@/hooks/useAccessRequest";
import type { RequestedPlan } from "@/lib/accessRequestOptions";

type Fase = "verificando" | "ok" | "error";

/** Compromiso de respuesta que se le declara al solicitante. */
const TIEMPO_DE_RESPUESTA = "dentro de los próximos 3 días hábiles";

function SolicitudVerificada() {
  const searchParams = useSearchParams();
  const token = searchParams.get("token") ?? "";

  const verificar = useVerifyAccessRequest();
  const [fase, setFase] = useState<Fase>("verificando");
  const [plan, setPlan] = useState<RequestedPlan | null>(null);
  const llamadoRef = useRef(false);

  useEffect(() => {
    if (llamadoRef.current || !token) {
      if (!token) setFase("error");
      return;
    }
    llamadoRef.current = true;

    verificar.mutate(token, {
      onSuccess: (data) => {
        setPlan(data.requested_plan);
        setFase("ok");
      },
      onError: () => setFase("error"),
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  if (fase === "verificando") {
    return (
      <div className="flex flex-col items-center gap-4 text-center">
        <div className="h-10 w-10 animate-spin rounded-full border-4 border-vk-blue/20 border-t-vk-blue" />
        <p className="text-[15px] text-vk-text-muted">Confirmando tu correo...</p>
      </div>
    );
  }

  if (fase === "error") {
    return (
      <div className="space-y-6 text-center">
        <div className="flex justify-center">
          <div className="flex h-16 w-16 items-center justify-center rounded-full bg-vk-danger-bg">
            <svg
              className="h-8 w-8 text-vk-danger"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
              aria-hidden="true"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z"
              />
            </svg>
          </div>
        </div>
        <div>
          <h1 className="text-2xl font-bold text-vk-text-primary">
            Link inválido o vencido
          </h1>
          <p className="mt-2 text-[15px] text-vk-text-muted">
            El link ya se usó o pasaron más de 48 horas. Podés pedir uno nuevo desde la
            pantalla de solicitud enviada, o mandar el formulario otra vez.
          </p>
        </div>
        <Link
          href="/solicitar-acceso"
          className="inline-flex items-center justify-center rounded-lg bg-vk-blue px-5 py-3 text-[15px] font-semibold text-white transition-colors hover:bg-vk-blue-hover"
        >
          Volver al formulario
        </Link>
      </div>
    );
  }

  return (
    <div className="space-y-6 text-center">
      <div className="flex justify-center">
        <div className="flex h-16 w-16 items-center justify-center rounded-full bg-vk-success-bg">
          <svg
            className="h-8 w-8 text-vk-success"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={2}
            aria-hidden="true"
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
          </svg>
        </div>
      </div>

      <div>
        <h1 className="text-2xl font-bold text-vk-text-primary">Correo confirmado</h1>
        {plan === "premium" ? (
          <p className="mt-2 text-[15px] text-vk-text-muted">
            Recibimos tu solicitud Premium. Vamos a revisar los datos y la compatibilidad
            de tu negocio antes de habilitar la cuenta.
          </p>
        ) : (
          <p className="mt-2 text-[15px] text-vk-text-muted">
            Tu solicitud ya está en revisión. Miramos los datos de tu negocio para ver si
            Véktor te sirve tal como está hoy.
          </p>
        )}
        <p className="mt-3 text-[15px] text-vk-text-secondary">
          Te escribimos a tu correo con la respuesta {TIEMPO_DE_RESPUESTA}. No hace falta
          que hagas nada más.
        </p>
      </div>

      <p className="text-sm text-vk-text-muted">
        <Link
          href="/"
          className="font-medium text-vk-blue hover:text-vk-blue-hover hover:underline"
        >
          Volver al inicio
        </Link>
      </p>
    </div>
  );
}

export default function SolicitudVerificadaPage() {
  return (
    <main className="min-h-screen md:grid md:grid-cols-2">
      <div className="hidden flex-col bg-vk-bg-dark px-12 py-12 md:flex">
        <div className="flex-1">
          <VektorLogo variant="full" size="lg" theme="dark" />
          <p className="mt-3 text-base text-vk-text-muted">
            No trabajes más. Decidí mejor.
          </p>
        </div>
      </div>

      <div className="flex min-h-screen items-center justify-center bg-vk-surface-w px-8 py-12 md:min-h-0 md:overflow-y-auto">
        <div className="w-full max-w-[420px]">
          <div className="mb-8 md:hidden">
            <VektorLogo variant="full" size="md" theme="light" />
          </div>

          <Suspense>
            <SolicitudVerificada />
          </Suspense>
        </div>
      </div>
    </main>
  );
}
