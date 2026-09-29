"use client";

/**
 * El turno de caja de esta PC para el usuario logueado (B8).
 *
 * Reglas, con su motivo:
 *
 * - **Sin turno no se cobra.** La pantalla muestra la apertura, con el fondo
 *   inicial, antes que el carrito.
 * - **La apertura puede ser sin red.** El id lo genera la caja y se guarda
 *   ANTES de mandar; si el servidor no contesta, la caja cobra igual y la
 *   apertura se reintenta sola. El servidor declara el fondo aunque una venta
 *   del turno haya llegado primero (`float_declared_at`), así que el orden
 *   entre la apertura y las ventas no importa.
 * - **El fondo que vale es el del servidor.** Si otra apertura del mismo turno
 *   declaró otro fondo antes, el servidor devuelve el real y la caja lo toma;
 *   no se muestra lo que se mandó como si hubiera funcionado.
 * - **Un turno que el encargado cerró se suelta.** Si al confirmar el estado el
 *   servidor dice CLOSED o ABANDONED, se borra el guardado y se vuelve a la
 *   apertura. Las ventas que ya estaban en la cola con ese id llegan como
 *   tardías: es lo correcto, cobraron en ese turno.
 * - **Nada de esto frena la caja por un error del servidor.** Un fallo al
 *   confirmar deja el turno como estaba; sólo una respuesta que dice "cerrado"
 *   lo suelta.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { centsToAmount, newOperationId, toCents } from "@/lib/pos/cart";
import { type PosCashSession, posService } from "@/services/pos.service";
import {
  type TurnoLocal,
  claveDeTurno,
  usePosCashSessionStore,
} from "@/stores/posCashSessionStore";

/** Cada cuánto se reintenta una apertura que no llegó. */
const REINTENTO_MS = 20_000;
/** Cada cuánto la caja pregunta si su turno sigue abierto. */
const VERIFICACION_MS = 60_000;

export interface EstadoDeTurno {
  turno: TurnoLocal | null;
  /** Por qué se soltó el turno, si lo cerró otro (el encargado). */
  avisoDeCierre: string | null;
  /** Turnos abiertos anteriores de este usuario en esta caja (sin contar éste). */
  anterioresAbiertos: number;
  abrir: (fondoCents: number) => Promise<void>;
  /** Reintenta la apertura pendiente. Devuelve el turno del servidor si llegó. */
  confirmarApertura: () => Promise<PosCashSession | null>;
  /** Olvida el turno de esta PC (después de cerrarlo). */
  soltar: () => void;
}

export interface OpcionesDeTurno {
  tenantId: string;
  userId: string;
  terminalId: string | null;
  habilitado?: boolean;
  automatico?: boolean;
}

export function centavosDeMonto(monto: string | null | undefined): number | null {
  if (monto === null || monto === undefined) return null;
  const texto = String(monto).trim();
  const negativo = texto.startsWith("-");
  const cents = toCents(negativo ? texto.slice(1) : texto);
  if (cents === null) return null;
  return negativo ? -cents : cents;
}

export function useCashSession({
  tenantId,
  userId,
  terminalId,
  habilitado = true,
  automatico = true,
}: OpcionesDeTurno): EstadoDeTurno {
  const clave = claveDeTurno(tenantId, userId, terminalId);
  const turno = usePosCashSessionStore((s) => (tenantId && userId ? s.turnos[clave] : undefined));
  const guardar = usePosCashSessionStore((s) => s.guardar);
  const actualizar = usePosCashSessionStore((s) => s.actualizar);
  const quitar = usePosCashSessionStore((s) => s.quitar);
  const [anterioresAbiertos, setAnteriores] = useState(0);
  const [avisoDeCierre, setAvisoDeCierre] = useState<string | null>(null);
  // La apertura en vuelo, compartida: si el reintento automático está mandando
  // la apertura justo cuando el cajero firma el cierre, el cierre la ESPERA en
  // vez de fallar por "no se pudo avisar la apertura".
  const enVuelo = useRef<Promise<PosCashSession | null> | null>(null);

  /** Toma el turno del servidor: su id, su fondo REAL y si sigue abierto. */
  const adoptarRespuesta = useCallback(
    (clientSessionId: string, sesion: PosCashSession | undefined): void => {
      if (!sesion || typeof sesion !== "object") return;
      // Sólo la respuesta de ESTE turno: tomar la de otro pisaría el fondo, o
      // soltaría un turno abierto porque otro está cerrado.
      if (sesion.client_session_id !== clientSessionId) return;
      const actual = usePosCashSessionStore.getState().turnos[clave];
      if (!actual || actual.clientSessionId !== clientSessionId) return;
      if (sesion.status !== "OPEN") {
        // Lo cerró otro —el encargado desde el panel, típicamente—. Se suelta,
        // pero NO en silencio: el cajero tiene que saber que su cajón ya no
        // tiene arqueo propio y avisarle al encargado.
        quitar(clave);
        setAvisoDeCierre(
          sesion.status === "ABANDONED"
            ? `El encargado cerró tu turno sin arqueo${
                sesion.notes ? ` (${sesion.notes})` : ""
              }. Lo que cobraste sigue registrado; avisale cuánto hay en el cajón.`
            : "Tu turno ya estaba cerrado. Abrí uno nuevo para seguir cobrando.",
        );
        return;
      }
      const real = centavosDeMonto(sesion.opening_float_ars);
      actualizar(clave, {
        serverId: sesion.id,
        aperturaConfirmada: true,
        openingFloatCents: real ?? actual.openingFloatCents,
      });
    },
    [clave, actualizar, quitar],
  );

  const confirmarApertura = useCallback((): Promise<PosCashSession | null> => {
    const actual = usePosCashSessionStore.getState().turnos[clave];
    if (!actual) return Promise.resolve(null);
    if (enVuelo.current) return enVuelo.current;
    const envio = (async () => {
      try {
        const sesion = await posService.openCashSession(
          actual.clientSessionId,
          centsToAmount(actual.declaredFloatCents),
        );
        adoptarRespuesta(actual.clientSessionId, sesion);
        return sesion ?? null;
      } catch {
        // Sin red, o el servidor no la pudo tomar todavía: se reintenta sola.
        return null;
      } finally {
        enVuelo.current = null;
      }
    })();
    enVuelo.current = envio;
    return envio;
  }, [clave, adoptarRespuesta]);

  const abrir = useCallback(
    async (fondoCents: number) => {
      if (!tenantId || !userId) return;
      setAvisoDeCierre(null);
      // PRIMERO se guarda: el turno existe desde acá, con o sin red.
      guardar(clave, {
        clientSessionId: newOperationId(),
        serverId: null,
        openingFloatCents: fondoCents,
        declaredFloatCents: fondoCents,
        openedAt: new Date().toISOString(),
        aperturaConfirmada: false,
      });
      await confirmarApertura();
    },
    [tenantId, userId, clave, guardar, confirmarApertura],
  );

  const soltar = useCallback(() => quitar(clave), [clave, quitar]);

  // Al montar (y al cambiar de usuario o caja): ¿el turno guardado sigue
  // abierto? Y ¿hay turnos anteriores sin cerrar en esta caja?
  const clientSessionId = turno?.clientSessionId ?? null;
  const serverId = turno?.serverId ?? null;
  useEffect(() => {
    if (!habilitado || !tenantId || !userId) return;
    let vigente = true;
    void (async () => {
      if (serverId && clientSessionId) {
        try {
          const sesion = await posService.getCashSession(serverId);
          if (vigente) adoptarRespuesta(clientSessionId, sesion);
        } catch {
          // Sin red: el turno sigue como estaba.
        }
      }
      try {
        const abiertos = await posService.listCashSessions({ open: true, terminalId });
        if (!vigente || !Array.isArray(abiertos)) return;
        setAnteriores(
          abiertos.filter(
            (s) => s.user_id === userId && s.client_session_id !== clientSessionId,
          ).length,
        );
      } catch {
        // Informativo: sin red no se muestra.
      }
    })();
    return () => {
      vigente = false;
    };
    // `serverId` fuera a propósito: confirmar la apertura lo setea y no hace
    // falta volver a preguntar en el acto.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [habilitado, tenantId, userId, terminalId, clientSessionId, adoptarRespuesta]);

  // ¿Sigue abierto mi turno? Se pregunta cada tanto y al volver la red: el
  // encargado puede cerrarlo desde otra PC, y enterarse recién al cerrar haría
  // que el cajero siga vendiendo horas en un turno que ya no tiene arqueo.
  useEffect(() => {
    if (!habilitado || !automatico || !serverId || !clientSessionId) return;
    const verificar = () => {
      void posService
        .getCashSession(serverId)
        .then((sesion) => adoptarRespuesta(clientSessionId, sesion))
        .catch(() => {
          // Sin red: se pregunta la próxima.
        });
    };
    window.addEventListener("online", verificar);
    const timer = window.setInterval(verificar, VERIFICACION_MS);
    return () => {
      window.removeEventListener("online", verificar);
      window.clearInterval(timer);
    };
  }, [habilitado, automatico, serverId, clientSessionId, adoptarRespuesta]);

  // La apertura que no llegó se reintenta: al volver la red y cada tanto.
  const pendiente = turno ? !turno.aperturaConfirmada : false;
  useEffect(() => {
    if (!habilitado || !automatico || !pendiente) return;
    const reintentar = () => void confirmarApertura();
    window.addEventListener("online", reintentar);
    const timer = window.setInterval(reintentar, REINTENTO_MS);
    return () => {
      window.removeEventListener("online", reintentar);
      window.clearInterval(timer);
    };
  }, [habilitado, automatico, pendiente, confirmarApertura]);

  return {
    turno: turno ?? null,
    avisoDeCierre,
    anterioresAbiertos,
    abrir,
    confirmarApertura,
    soltar,
  };
}
