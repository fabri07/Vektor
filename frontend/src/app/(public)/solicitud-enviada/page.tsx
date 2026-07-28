"use client";

/**
 * `/solicitud-enviada` — pantalla posterior al envío del formulario.
 *
 * Dice UNA cosa: revisá tu correo. La solicitud todavía no entró a la cola de
 * revisión — entra recién cuando el visitante confirma su email (doble opt-in),
 * y decirlo evita que se quede esperando una respuesta que nunca va a llegar.
 *
 * El reenvío responde SIEMPRE lo mismo, exista o no una solicitud con ese
 * email: distinguir los casos sería un oráculo de "este correo pidió acceso".
 */

import { Suspense, useEffect, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";

import { VektorLogo } from "@/components/ui/VektorLogo";
import { resendAccessRequestVerification } from "@/services/accessRequest.service";

const RESEND_COOLDOWN = 60;

function SolicitudEnviada() {
  const searchParams = useSearchParams();
  const email = searchParams.get("email") ?? "";

  const [estado, setEstado] = useState<"idle" | "enviando" | "enviado" | "error">("idle");
  const [restante, setRestante] = useState(RESEND_COOLDOWN);

  useEffect(() => {
    if (restante <= 0) return;
    const id = setTimeout(() => setRestante((c) => c - 1), 1000);
    return () => clearTimeout(id);
  }, [restante]);

  async function reenviar() {
    if (!email) return;
    setEstado("enviando");
    try {
      await resendAccessRequestVerification(email);
      setEstado("enviado");
      setRestante(RESEND_COOLDOWN);
    } catch {
      setEstado("error");
    }
  }

  const puedeReenviar = restante <= 0 && estado !== "enviando" && !!email;

  return (
    <div className="space-y-6 text-center">
      <div className="flex justify-center">
        <div className="flex h-16 w-16 items-center justify-center rounded-full bg-vk-info-bg">
          <svg
            className="h-8 w-8 text-vk-blue"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={1.5}
            aria-hidden="true"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M21.75 6.75v10.5a2.25 2.25 0 01-2.25 2.25h-15a2.25 2.25 0 01-2.25-2.25V6.75m19.5 0A2.25 2.25 0 0019.5 4.5h-15a2.25 2.25 0 00-2.25 2.25m19.5 0v.243a2.25 2.25 0 01-1.07 1.916l-7.5 4.615a2.25 2.25 0 01-2.36 0L3.32 8.91a2.25 2.25 0 01-1.07-1.916V6.75"
            />
          </svg>
        </div>
      </div>

      <div>
        <h1 className="text-2xl font-bold text-vk-text-primary">Revisá tu correo</h1>
        <p className="mt-2 text-[15px] text-vk-text-muted">
          Recibimos tu solicitud. Te mandamos un link a{" "}
          {email ? (
            <span className="font-medium text-vk-text-secondary">{email}</span>
          ) : (
            "tu dirección de email"
          )}{" "}
          para que confirmes que es tuyo.
        </p>
        <p className="mt-2 text-sm text-vk-text-muted">
          Hasta que lo confirmes, tu solicitud no entra a la cola de revisión. El link
          vale 48 horas.
        </p>
      </div>

      <div className="rounded-lg border border-vk-border-w bg-vk-bg-light px-5 py-4 text-sm text-vk-text-secondary">
        {estado === "enviado" ? (
          <>
            <p className="font-medium text-vk-success">
              Listo. Si hay una solicitud pendiente de confirmar con ese correo, te
              reenviamos el link.
            </p>
            {restante > 0 && (
              <p className="mt-1.5 text-vk-text-muted">Podés pedir otro en {restante}s</p>
            )}
          </>
        ) : estado === "error" ? (
          <>
            <p className="text-vk-danger">
              No pudimos reenviarlo. Probá de nuevo en unos minutos.
            </p>
            {puedeReenviar && (
              <button
                type="button"
                onClick={reenviar}
                className="mt-1.5 font-medium text-vk-blue hover:text-vk-blue-hover hover:underline"
              >
                Reintentar
              </button>
            )}
          </>
        ) : restante > 0 ? (
          <p>
            ¿No te llegó? Revisá la carpeta de spam.{" "}
            <span className="text-vk-text-muted">Podés pedir otro en {restante}s.</span>
          </p>
        ) : (
          <>
            <p>¿No te llegó? Revisá la carpeta de spam o</p>
            <button
              type="button"
              onClick={reenviar}
              disabled={!puedeReenviar}
              className="mt-1.5 font-medium text-vk-blue hover:text-vk-blue-hover hover:underline disabled:cursor-not-allowed disabled:opacity-50"
            >
              {estado === "enviando" ? "Enviando..." : "Reenviar el link de confirmación"}
            </button>
          </>
        )}
      </div>

      <p className="text-sm text-vk-text-muted">
        <Link href="/" className="font-medium text-vk-blue hover:text-vk-blue-hover hover:underline">
          Volver al inicio
        </Link>
      </p>
    </div>
  );
}

export default function SolicitudEnviadaPage() {
  return (
    <main className="min-h-screen md:grid md:grid-cols-2">
      <div className="hidden flex-col bg-vk-bg-dark px-12 py-12 md:flex">
        <div className="flex-1">
          <VektorLogo variant="full" size="lg" theme="dark" />
          <p className="mt-3 text-base text-vk-text-muted">
            Un paso más y tu solicitud entra a revisión.
          </p>
        </div>
      </div>

      <div className="flex min-h-screen items-center justify-center bg-vk-surface-w px-8 py-12 md:min-h-0 md:overflow-y-auto">
        <div className="w-full max-w-[420px]">
          <div className="mb-8 md:hidden">
            <VektorLogo variant="full" size="md" theme="light" />
          </div>

          <Suspense>
            <SolicitudEnviada />
          </Suspense>
        </div>
      </div>
    </main>
  );
}
