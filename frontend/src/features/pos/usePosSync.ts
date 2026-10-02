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
  actualizarAprendizaje,
  aprendizajesSincronizables,
  listarAprendizajes,
} from "@/lib/offline/aprendizajes";
import {
  type AprendizajeGuardado,
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

/**
 * Lo que dice el servidor de un código aprendido sin red y que NO se arregla
 * reintentando: otro producto ya lo tiene, el producto ya tiene otro, el código
 * no es aprendible, o el producto ya no existe. Va a revisión.
 */
const RECHAZOS_DE_APRENDIZAJE = new Set([
  "BARCODE_TAKEN",
  "BARCODE_ALREADY_SET",
  "SCAN_CODE_NOT_LEARNABLE",
]);

export interface EstadoDeSync {
  /** Todo lo que todavía no está en el servidor, en orden de cobro. */
  pendientes: OperacionGuardada[];
  /** Códigos aprendidos sin red que todavía no subieron o quedaron en revisión. */
  aprendizajes: AprendizajeGuardado[];
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
  const [aprendizajes, setAprendizajes] = useState<AprendizajeGuardado[]>([]);
  const [sincronizando, setSincronizando] = useState(false);
  const datos = useRef({ tenantId, userId, terminalId });
  datos.current = { tenantId, userId, terminalId };

  const refrescar = useCallback(async () => {
    if (!datos.current.tenantId) return;
    setPendientes(await listarNoSincronizadas(datos.current.tenantId));
    setAprendizajes(
      (await listarAprendizajes(datos.current.tenantId)).filter(
        (a) => a.estado !== "SINCRONIZADO",
      ),
    );
  }, []);

  const flush = useCallback(async () => {
    const { tenantId: t, userId: u, terminalId: term } = datos.current;
    if (!t || !u) return;
    setSincronizando(true);
    try {
      await conCandadoDeSync(async () => {
        let sinRed = false;
        const cola = await pendientesSincronizables(t, u, term);
        for (const op of cola) {
          try {
            // Una venta migrada de B13 se RECLAMA primero con su contenido
            // original y sin el header: su primer envío ya ocurrió y puede
            // existir en el servidor. Ver `adaptarMigradaDeB13`.
            const resultado = await posService.syncOperation(op.payload, {
              offline: op.origen !== "b13",
            });
            await actualizarOperacion(op.clientOperationId, {
              estado: "SINCRONIZADA",
              resultado,
              sincronizadaEn: new Date().toISOString(),
              ultimoError: null,
              ultimoCodigo: null,
            });
          } catch (e) {
            const info = posErrorInfo(e);
            if (op.origen === "b13" && info.code === "INSUFFICIENT_STOCK") {
              // Llegar al chequeo de stock PRUEBA que la venta no existía: una
              // clave ya reclamada con el mismo contenido se contesta antes,
              // con el ticket original. Y el 400 revierte la transacción, así
              // que la clave queda libre. Recién ahora se adapta: se declara
              // `charged_at` —el mismo instante que congeló B13 al cobrar— y el
              // próximo intento va por el camino offline y captura la salida
              // pendiente.
              await actualizarOperacion(op.clientOperationId, {
                origen: undefined,
                payload: { ...op.payload, charged_at: op.payload.operation_date },
                intentos: op.intentos + 1,
                ultimoError: null,
                ultimoCodigo: null,
                proximoIntentoEn: 0,
              });
              continue;
            }
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
              if (red) {
                sinRed = true;
                break;
              }
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
        // Después de las ventas, los códigos aprendidos sin red. Un conflicto
        // NO toca ninguna venta: lo vendido con ese código se vendió con el
        // producto que el cajero eligió, que es lo que se cobró.
        if (sinRed) return;
        for (const a of await aprendizajesSincronizables(t, u, term)) {
          try {
            await posService.learnBarcode(a.productId, a.codigo, { offline: true });
            await actualizarAprendizaje(a.id, {
              estado: "SINCRONIZADO",
              ultimoError: null,
              ultimoCodigo: null,
            });
          } catch (e) {
            const info = posErrorInfo(e);
            if (esFalloDeRed(info)) break;
            // Lo que el servidor rechaza para siempre —el código ya es de otro,
            // el permiso fue revocado, un 404 o un 422— va a revisión y deja de
            // superponerse: si siguiera pendiente, esta PC resolvería ese código
            // a ese producto indefinidamente. Sesión o caja (autenticación) y
            // errores pasajeros se reintentan.
            if (
              (info.code !== null && RECHAZOS_DE_APRENDIZAJE.has(info.code)) ||
              clasificarSync(info) === "revision"
            ) {
              await actualizarAprendizaje(a.id, {
                estado: "REQUIERE_REVISION",
                intentos: a.intentos + 1,
                ultimoError: info.message ?? "El servidor no aceptó el código.",
                ultimoCodigo: info.code,
              });
            } else {
              // Sesión vencida, caja dada de baja o error del servidor: se
              // reintenta más tarde, sin perder el aprendizaje.
              const intentos = a.intentos + 1;
              await actualizarAprendizaje(a.id, {
                intentos,
                ultimoError: info.message ?? "No se pudo subir el código.",
                ultimoCodigo: info.code,
                proximoIntentoEn: Date.now() + esperaPorIntentos(intentos),
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

  return { pendientes, aprendizajes, sincronizando, flush, refrescar };
}
