"use client";

/**
 * Sube al servidor las ventas que la caja ya cobró.
 *
 * Reglas, todas con su motivo:
 *
 * - **De a una y en orden de cobro.** Dos envíos en paralelo no duplican nada
 *   —el `client_operation_id` lo impide— pero sí desordenan el estado local.
 * - **Sólo las de esta sesión.** Tenant, usuario y caja tienen que coincidir:
 *   una venta que cobró Juan subida bajo la sesión de Ana se la adjudicaría a
 *   Ana, y el arqueo del turno quedaría mal para los dos.
 * - **Un fallo de red no gasta presupuesto de reintentos.** `navigator.onLine`
 *   da `true` con un router sin internet (lección de B0), así que si contara
 *   como intento, unos minutos de corte agotarían el tope y la primera
 *   respuesta real llegaría con la venta ya marcada como problemática.
 * - **Nada se borra automáticamente.** Una venta cobrada cambia de estado, y
 *   queda a la vista hasta que una persona decida.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import {
  type OperacionGuardada,
  actualizarOperacion,
  listarNoSincronizadas,
  listarOperaciones,
  pendientesSincronizables,
} from "@/lib/offline/posDb";
import { conCandadoDeSync } from "@/lib/offline/tabLock";
import { posErrorInfo } from "@/lib/pos/errors";
import { clasificarSync, esFalloDeRed } from "@/lib/pos/syncClassification";
import { posService } from "@/services/pos.service";

/** Cada cuánto se mira la cola sola, además de los eventos. */
const INTERVALO_MS = 20_000;
/** Un corte se resuelve solo: se reintenta seguido, sin castigar. */
const ESPERA_DE_RED_MS = 5_000;
const ESPERA_MAXIMA_MS = 5 * 60_000;

function esperaPorIntentos(intentos: number): number {
  return Math.min(ESPERA_MAXIMA_MS, 5_000 * 2 ** Math.max(0, intentos - 1));
}

export interface EstadoDeSync {
  /** Todo lo que todavía no está en el servidor, en orden de cobro. */
  pendientes: OperacionGuardada[];
  sincronizando: boolean;
  flush: () => Promise<void>;
  refrescar: () => Promise<void>;
}

export interface OpcionesDeSync {
  tenantId: string;
  userId: string;
  terminalId: string | null;
  /**
   * `false` mientras la caja no sea la pestaña dueña: dos pestañas subiendo la
   * misma cola no duplican ventas, pero se pisan el estado local.
   */
  habilitado?: boolean;
  /** `false` en los tests que no quieren el temporizador. */
  automatico?: boolean;
}

export function usePosSync({
  tenantId,
  userId,
  terminalId,
  habilitado = true,
  automatico = true,
}: OpcionesDeSync): EstadoDeSync {
  const [pendientes, setPendientes] = useState<OperacionGuardada[]>([]);
  const [sincronizando, setSincronizando] = useState(false);
  const datos = useRef({ tenantId, userId, terminalId });
  datos.current = { tenantId, userId, terminalId };

  const refrescar = useCallback(async () => {
    if (!datos.current.tenantId) return;
    setPendientes(await listarNoSincronizadas(datos.current.tenantId));
  }, []);

  const flush = useCallback(async () => {
    const { tenantId: t, userId: u, terminalId: term } = datos.current;
    if (!t || !u) return;
    setSincronizando(true);
    try {
      await conCandadoDeSync(async () => {
        const cola = await pendientesSincronizables(t, u, term);
        for (const op of cola) {
          try {
            const resultado = await posService.syncOperation(op.payload);
            await actualizarOperacion(op.clientOperationId, {
              estado: "SINCRONIZADA",
              resultado,
              sincronizadaEn: new Date().toISOString(),
              ultimoError: null,
              ultimoCodigo: null,
            });
          } catch (e) {
            const info = posErrorInfo(e);
            const clase = clasificarSync(info);
            const red = esFalloDeRed(info);
            if (clase === "reintento") {
              const intentos = red ? op.intentos : op.intentos + 1;
              await actualizarOperacion(op.clientOperationId, {
                intentos,
                ultimoError: info.message ?? (red ? "Sin conexión" : "Error del servidor"),
                ultimoCodigo: info.code,
                proximoIntentoEn:
                  Date.now() + (red ? ESPERA_DE_RED_MS : esperaPorIntentos(intentos)),
              });
              // Sin red no tiene sentido seguir con el resto de la cola: sería
              // gastar el mismo error una vez por venta.
              if (red) break;
            } else {
              await actualizarOperacion(op.clientOperationId, {
                estado:
                  clase === "autenticacion" ? "REQUIERE_AUTENTICACION" : "REQUIERE_REVISION",
                intentos: op.intentos + 1,
                ultimoError: info.message ?? "El servidor la rechazó",
                ultimoCodigo: info.code,
              });
            }
          }
        }
      });
    } finally {
      setSincronizando(false);
      await refrescar();
    }
  }, [refrescar]);

  /**
   * Volver a entrar reactiva lo que quedó esperando autenticación.
   *
   * `REQUIERE_AUTENTICACION` significa "la sesión venció" o "dieron de baja
   * esta caja". Montar la caja con una sesión y una terminal válidas ES volver
   * a entrar, así que esas ventas pasan a `PENDIENTE` y la cola las toma.
   */
  const revivir = useCallback(async () => {
    const { tenantId: t, userId: u, terminalId: term } = datos.current;
    if (!t || !u) return;
    for (const op of await listarOperaciones(t)) {
      if (
        op.estado === "REQUIERE_AUTENTICACION" &&
        op.userId === u &&
        op.terminalId === term
      ) {
        await actualizarOperacion(op.clientOperationId, {
          estado: "PENDIENTE",
          proximoIntentoEn: 0,
        });
      }
    }
  }, []);

  useEffect(() => {
    if (!habilitado || !tenantId || !userId) return;
    let vivo = true;
    void (async () => {
      await revivir();
      if (vivo) await flush();
    })();

    if (!automatico) {
      return () => {
        vivo = false;
      };
    }
    const alVolver = () => void flush();
    const alVerse = () => {
      if (document.visibilityState === "visible") void flush();
    };
    window.addEventListener("online", alVolver);
    document.addEventListener("visibilitychange", alVerse);
    const timer = window.setInterval(() => void flush(), INTERVALO_MS);
    return () => {
      vivo = false;
      window.removeEventListener("online", alVolver);
      document.removeEventListener("visibilitychange", alVerse);
      window.clearInterval(timer);
    };
  }, [habilitado, tenantId, userId, terminalId, automatico, flush, revivir]);

  return { pendientes, sincronizando, flush, refrescar };
}
