/**
 * E6c-3 — registrar una importación, seguirla, y retomarla si se recarga.
 *
 * Las tres cosas que este hook garantiza
 * --------------------------------------
 * 1. **Un timeout no duplica.** La clave se reserva antes de mandar, y un
 *    reintento usa la misma. Nunca se cae a `/confirm` salvo un 404 explícito
 *    (ver `permiteCaerAlConfirmSincronico`).
 * 2. **Recargar no pierde la importación.** El id queda guardado, así que al
 *    volver a la pantalla se retoma la consulta en vez de mostrar un archivo que
 *    parece no haber hecho nada.
 * 3. **Un error dice qué hacer.** Lo que se muestra es el `error_detail` del
 *    backend, que ya viene redactado como una acción; el `error_code` queda para
 *    distinguir casos sin leer texto.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { ingestionService, type ImportacionResponse } from "@/services/ingestion.service";

import {
  esTerminal,
  importacionEnCurso,
  olvidarImportacion,
  permiteCaerAlConfirmSincronico,
  proximaEspera,
  recordarIntento,
  reservarClave,
} from "./importacionAsincronica";

export interface EstadoDelSeguimiento {
  /** `null` mientras no haya ninguna importación en curso para este archivo. */
  importacion: ImportacionResponse | null;
  siguiendo: boolean;
  /** Un error del CLIENTE (red, consulta). Los del import viven en `importacion`. */
  errorDeRed: string | null;
}

export interface UseImportacionAsincronica extends EstadoDelSeguimiento {
  /**
   * Registra la importación. Devuelve `"no_habilitado"` cuando el backend
   * responde 404 — el único caso donde el llamador puede usar `/confirm` sin
   * riesgo de duplicar.
   */
  registrar: (payload: Record<string, unknown>) => Promise<"registrado" | "no_habilitado">;
  /** Deja de seguir y olvida la importación (cuando el usuario cierra el panel). */
  descartar: () => void;
}

export function useImportacionAsincronica(
  fileId: string | null,
  onTerminada?: (imp: ImportacionResponse) => void,
): UseImportacionAsincronica {
  const [importacion, setImportacion] = useState<ImportacionResponse | null>(null);
  const [siguiendo, setSiguiendo] = useState(false);
  const [errorDeRed, setErrorDeRed] = useState<string | null>(null);

  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const consultas = useRef(0);
  const vivo = useRef(true);
  // `onTerminada` en una ref: si fuera dependencia del efecto, un callback
  // recreado en cada render reiniciaría el seguimiento en cada render.
  const alTerminar = useRef(onTerminada);
  alTerminar.current = onTerminada;

  const detener = useCallback(() => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
    setSiguiendo(false);
  }, []);

  const consultar = useCallback(
    async (attemptId: string, archivoId: string) => {
      if (!vivo.current) return;
      try {
        const estado = await ingestionService.estadoDeImportacion(attemptId);
        if (!vivo.current) return;
        setImportacion(estado);
        setErrorDeRed(null);
        if (esTerminal(estado.status)) {
          detener();
          olvidarImportacion(archivoId);
          alTerminar.current?.(estado);
          return;
        }
      } catch (error) {
        if (!vivo.current) return;
        // Una consulta que falla NO termina el seguimiento: la importación sigue
        // corriendo del lado del servidor y la red puede volver. Cortar acá
        // dejaría al usuario mirando un estado congelado sin saber que lo es.
        const status = (error as { response?: { status?: number } })?.response?.status;
        if (status === 404) {
          // El intento no existe (o no es de este tenant): seguir consultando no
          // va a cambiar eso.
          detener();
          olvidarImportacion(archivoId);
          setErrorDeRed("No se encontró la importación.");
          return;
        }
        setErrorDeRed("Se perdió la conexión. Seguimos intentando…");
      }
      consultas.current += 1;
      timer.current = setTimeout(
        () => void consultar(attemptId, archivoId),
        proximaEspera(consultas.current),
      );
    },
    [detener],
  );

  const seguir = useCallback(
    (attemptId: string, archivoId: string) => {
      consultas.current = 0;
      setSiguiendo(true);
      void consultar(attemptId, archivoId);
    },
    [consultar],
  );

  // Recuperación al recargar: si quedó una importación abierta para este archivo,
  // se retoma. Sin esto, volver a la pantalla muestra un archivo que parece no
  // haber hecho nada mientras el servidor lo está importando.
  useEffect(() => {
    vivo.current = true;
    if (!fileId) return undefined;
    const abierta = importacionEnCurso(fileId);
    if (abierta?.attemptId) seguir(abierta.attemptId, fileId);
    return () => {
      vivo.current = false;
      if (timer.current) clearTimeout(timer.current);
    };
  }, [fileId, seguir]);

  const registrar = useCallback(
    async (payload: Record<string, unknown>): Promise<"registrado" | "no_habilitado"> => {
      if (!fileId) return "no_habilitado";
      // La clave se reserva ANTES de mandar: si viviera en memoria, un reload
      // durante el timeout la perdería y el reintento crearía un segundo intento.
      const { requestKey } = reservarClave(fileId);
      try {
        const respuesta = await ingestionService.registrarImportacion(
          fileId,
          requestKey,
          payload,
        );
        recordarIntento(fileId, respuesta.attempt_id);
        setImportacion(respuesta);
        setErrorDeRed(null);
        if (esTerminal(respuesta.status)) {
          olvidarImportacion(fileId);
          alTerminar.current?.(respuesta);
        } else {
          seguir(respuesta.attempt_id, fileId);
        }
        return "registrado";
      } catch (error) {
        if (permiteCaerAlConfirmSincronico(error)) {
          // 404: la ruta no está habilitada. El servidor no miró el cuerpo, así
          // que no hay nada registrado y el confirm sincrónico es seguro.
          olvidarImportacion(fileId);
          return "no_habilitado";
        }
        // Cualquier otra cosa —timeout, red, 5xx— es INCERTIDUMBRE: la petición
        // pudo haberse registrado. No se cae a `/confirm`; se deja la clave
        // guardada para que el reintento sea la misma petición.
        throw error;
      }
    },
    [fileId, seguir],
  );

  const descartar = useCallback(() => {
    detener();
    if (fileId) olvidarImportacion(fileId);
    setImportacion(null);
  }, [detener, fileId]);

  return { importacion, siguiendo, errorDeRed, registrar, descartar };
}
