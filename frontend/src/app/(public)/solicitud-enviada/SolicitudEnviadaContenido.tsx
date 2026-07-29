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
 *
 * **Nunca es un callejón sin salida.** Esta pantalla afirma que mandamos un
 * mail, y hay un caso en el que eso no es cierto sin que nadie pueda saberlo
 * desde acá: si el anti-bot del backend marcó el envío (el honeypot lo llena a
 * veces el autofill del navegador), no se persistió ninguna solicitud y la
 * respuesta fue el mismo 201 genérico. Distinguirlo del lado del cliente
 * rompería la neutralidad de enumeración, así que no se intenta — pero la
 * salida está siempre visible: volver a mandar el formulario, que sirve igual
 * para el falso positivo del anti-bot, para el mail que se perdió y para el
 * prefill de Google que venció. Por el mismo motivo el reenvío es reusable:
 * cuando el cooldown termina, el botón vuelve.
 */

import { useEffect, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";

import { resendAccessRequestVerification } from "@/services/accessRequest.service";

const RESEND_COOLDOWN = 60;

export function SolicitudEnviada() {
  const searchParams = useSearchParams();
  const email = searchParams.get("email") ?? "";

  const [estado, setEstado] = useState<"idle" | "enviando" | "enviado" | "error">("idle");
  const [restante, setRestante] = useState(RESEND_COOLDOWN);

  useEffect(() => {
    if (restante <= 0) {
      /*
       * El contador dice "podés pedir otro en N segundos". Cumplir esa promesa
       * es devolver el botón, no solo dejar de contar: `"enviado"` es la
       * primera rama del render y sin esto queda terminal — el visitante ve un
       * cartel de éxito, el contador llega a cero, y no aparece nada.
       */
      setEstado((e) => (e === "enviado" ? "idle" : e));
      return;
    }
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

  const hayEmail = !!email;
  const puedeReenviar = restante <= 0 && estado !== "enviando" && hayEmail;

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
        {!hayEmail ? (
          /*
           * Sin `?email` el reenvío no tiene a dónde ir (un bookmark, un
           * refresh que perdió la query, el historial). Un botón gris para
           * siempre y sin explicación es peor que no tenerlo: acá se dice por
           * qué, y la salida de abajo sigue sirviendo.
           */
          <p>
            Para reenviarte el link necesitamos saber a qué dirección lo mandamos, y
            esta pantalla ya no la tiene.
          </p>
        ) : estado === "enviado" ? (
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

        {/*
          Salida permanente, en todas las ramas. Es lo único que rescata al
          visitante cuyo envío quedó descartado por el anti-bot: para él no hay
          solicitud que reenviar, y el reenvío le va a responder que sí igual
          (neutralidad de enumeración). Volver a mandar el formulario es
          idempotente del lado del backend y reemite el token.
        */}
        <p className="mt-3 border-t border-vk-border-w pt-3 text-vk-text-muted">
          ¿Pasaron unos minutos y no llegó nada?{" "}
          <Link
            href="/solicitar-acceso"
            className="font-medium text-vk-blue hover:text-vk-blue-hover hover:underline"
          >
            Volvé a mandar el formulario
          </Link>
          .
        </p>
      </div>

      {/*
        Qué sigue después de confirmar. Sin esto la pantalla dice "revisá tu
        correo" y corta: el visitante no sabe si después hay que hacer algo
        más, cuánto puede tardar, ni qué pasa si la respuesta es que no.
      */}
      <div className="rounded-xl border border-vk-border-w bg-vk-bg-light p-4 text-sm text-vk-text-secondary">
        <p className="font-medium text-vk-text-primary">Qué sigue</p>
        <ol className="mt-2 list-decimal space-y-1 pl-4 text-vk-text-muted">
          <li>Confirmás tu email con el link que te mandamos.</li>
          <li>Ahí tu solicitud entra a la cola y la revisamos a mano.</li>
          <li>
            Te escribimos con la respuesta dentro de los próximos 3 días hábiles —
            sea que sí o que no. No hace falta que hagas nada más.
          </li>
        </ol>
      </div>

      <p className="text-sm text-vk-text-muted">
        <Link href="/" className="font-medium text-vk-blue hover:text-vk-blue-hover hover:underline">
          Volver al inicio
        </Link>
      </p>
    </div>
  );
}
