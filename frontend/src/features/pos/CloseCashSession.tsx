"use client";

import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/Button";
import { centavosDeMonto } from "@/features/pos/useCashSession";
import { centsToAmount, formatCents, toCents } from "@/lib/pos/cart";
import { posErrorInfo } from "@/lib/pos/errors";
import { type PosCashSession, posService } from "@/services/pos.service";
import type { TurnoLocal } from "@/stores/posCashSessionStore";

export interface PendientesDelTurno {
  /** Cobradas y todavía no subidas. */
  sinSincronizar: number;
  /** Rechazadas por algo corregible y no rehechas: tienen `charged_at`
   *  afirmado, así que puede haber plata de ellas en el cajón. */
  enCorreccion: number;
}

interface CloseCashSessionProps {
  turno: TurnoLocal;
  pendientes: PendientesDelTurno;
  /** Sube la cola antes de contar. */
  onSincronizar: () => Promise<void>;
  confirmarApertura: () => Promise<PosCashSession | null>;
  /** El turno ya está cerrado (por este cierre o por el encargado). */
  onTerminado: () => void;
  onCancelar: () => void;
}

type Paso = "preparando" | "contar" | "resultado";

/**
 * Cerrar el turno con arqueo CIEGO (B8).
 *
 * El cajero carga lo que contó sin ver cuánto "debería" haber: si lo viera, el
 * conteo se acomodaría al número y el arqueo dejaría de probar algo. El
 * servidor tampoco se lo manda mientras el turno está abierto. Después de
 * firmar se muestra todo: fondo, esperado, contado y diferencia.
 *
 * **Un cierre no se reintenta a ciegas.** El segundo cierre trae OTRO conteo y
 * el servidor lo rechaza (409). Si la respuesta se perdió, antes de volver a
 * mandar se pregunta cómo quedó el turno: si ya está cerrado, se muestra lo
 * firmado.
 */
export function CloseCashSession({
  turno,
  pendientes,
  onSincronizar,
  confirmarApertura,
  onTerminado,
  onCancelar,
}: CloseCashSessionProps) {
  const [paso, setPaso] = useState<Paso>("preparando");
  const [contado, setContado] = useState("");
  const [notas, setNotas] = useState("");
  const [enviando, setEnviando] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [resultado, setResultado] = useState<PosCashSession | null>(null);
  const [online, setOnline] = useState(
    typeof navigator === "undefined" ? true : navigator.onLine,
  );
  // Un envío que no tuvo respuesta: el próximo intento primero pregunta.
  const quizasFirmado = useRef(false);

  useEffect(() => {
    const on = () => setOnline(true);
    const off = () => setOnline(false);
    window.addEventListener("online", on);
    window.addEventListener("offline", off);
    return () => {
      window.removeEventListener("online", on);
      window.removeEventListener("offline", off);
    };
  }, []);

  useEffect(() => {
    let vigente = true;
    void (async () => {
      try {
        await onSincronizar();
      } finally {
        if (vigente) setPaso("contar");
      }
    })();
    return () => {
      vigente = false;
    };
    // Una sola vez al abrir el cierre.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const cents = contado.trim() === "" ? null : toCents(contado);
  const hayPendientes = pendientes.sinSincronizar + pendientes.enCorreccion > 0;

  async function yaCerrado(serverId: string): Promise<boolean> {
    try {
      const sesion = await posService.getCashSession(serverId);
      if (sesion && sesion.status !== "OPEN") {
        setResultado(sesion);
        setPaso("resultado");
        return true;
      }
    } catch {
      // Si no se puede saber, no se manda otro conteo a ciegas.
      setError("No se pudo confirmar cómo quedó el turno. Revisá la conexión y volvé a intentar.");
      return true;
    }
    return false;
  }

  async function cerrar(): Promise<void> {
    if (cents === null) return;
    setEnviando(true);
    setError(null);
    try {
      let serverId = turno.serverId;
      if (!serverId || !turno.aperturaConfirmada) {
        const sesion = await confirmarApertura();
        serverId = sesion?.id ?? null;
      }
      if (!serverId) {
        setError("No se pudo avisar la apertura del turno al servidor. Revisá la conexión.");
        return;
      }
      if (quizasFirmado.current && (await yaCerrado(serverId))) return;
      try {
        const r = await posService.closeCashSession(
          serverId,
          centsToAmount(cents),
          notas.trim() || null,
        );
        quizasFirmado.current = false;
        setResultado(r);
        setPaso("resultado");
      } catch (e) {
        const info = posErrorInfo(e);
        if (info.code === "CASH_SESSION_ALREADY_CLOSED") {
          // Lo cerró el encargado, o un envío anterior sí llegó: se muestra lo
          // firmado y no se manda otro conteo.
          await yaCerrado(serverId);
          return;
        }
        if (info.status === null) {
          quizasFirmado.current = true;
          setError("No hubo respuesta del servidor. Volvé a intentar: primero se revisa si ya se cerró.");
          return;
        }
        setError(info.message ?? "No se pudo cerrar el turno.");
      }
    } finally {
      setEnviando(false);
    }
  }

  if (paso === "resultado" && resultado) {
    return <ResultadoDelArqueo sesion={resultado} onListo={onTerminado} />;
  }

  return (
    <div className="flex flex-1 items-center justify-center p-6">
      <form
        className="w-full max-w-md space-y-4 rounded-xl border border-pos-border bg-pos-surface p-6"
        onSubmit={(e) => {
          e.preventDefault();
          void cerrar();
        }}
      >
        <h2 className="text-lg font-semibold text-pos-text">Cerrar turno</h2>
        {paso === "preparando" ? (
          <p className="text-sm text-pos-text-2">Subiendo las ventas del turno…</p>
        ) : (
          <>
            {hayPendientes ? (
              <div className="space-y-1 rounded-lg border border-pos-border p-3 text-sm" role="status">
                <p className="text-pos-danger">Hay ventas de este turno que no llegaron al servidor:</p>
                {pendientes.sinSincronizar > 0 ? (
                  <p className="text-pos-text" data-testid="cierre-sin-sincronizar">
                    {pendientes.sinSincronizar} sin sincronizar
                  </p>
                ) : null}
                {pendientes.enCorreccion > 0 ? (
                  <p className="text-pos-text" data-testid="cierre-en-correccion">
                    {pendientes.enCorreccion} sin completar (en corrección): se afirmó el cobro,
                    así que puede haber plata de ellas en el cajón
                  </p>
                ) : null}
                <p className="text-pos-text-2">
                  No entran en este arqueo. Si cerrás igual, cuando lleguen van a figurar como
                  ventas tardías del turno.
                </p>
              </div>
            ) : null}
            <p className="text-sm text-pos-text-2">
              Contá el efectivo del cajón y escribí cuánto hay. El sistema compara recién después
              de que confirmes.
            </p>
            <label className="block space-y-1 text-sm text-pos-text">
              <span>Efectivo contado en el cajón</span>
              <input
                aria-label="Efectivo contado"
                inputMode="decimal"
                autoFocus
                data-pos-scan="capture"
                placeholder="0,00"
                value={contado}
                onChange={(e) => setContado(e.target.value)}
                className="w-full rounded-lg border border-pos-border bg-pos-field px-3 py-2 text-right text-lg text-pos-text"
              />
            </label>
            {contado.trim() !== "" && cents === null ? (
              <p className="text-sm text-pos-danger">Escribí un monto válido.</p>
            ) : null}
            <textarea
              aria-label="Notas del cierre"
              placeholder="Notas (opcional)"
              maxLength={500}
              value={notas}
              onChange={(e) => setNotas(e.target.value)}
              className="w-full rounded-lg border border-pos-border bg-pos-field p-2 text-sm text-pos-text"
            />
            {!online ? (
              <p className="text-sm text-pos-danger">
                Sin conexión: el arqueo lo calcula el servidor, así que el turno se cierra cuando
                vuelva la red.
              </p>
            ) : null}
            {error ? (
              <p className="text-sm text-pos-danger" role="alert">
                {error}
              </p>
            ) : null}
          </>
        )}
        <div className="flex gap-2">
          <Button
            type="submit"
            className="flex-1"
            loading={enviando}
            disabled={paso !== "contar" || cents === null || !online || enviando}
          >
            {hayPendientes ? "Cerrar igual y firmar" : "Firmar el arqueo"}
          </Button>
          <Button type="button" variant="ghost" onClick={onCancelar} disabled={enviando}>
            Volver a la caja
          </Button>
        </div>
      </form>
    </div>
  );
}

function ResultadoDelArqueo({ sesion, onListo }: { sesion: PosCashSession; onListo: () => void }) {
  const monto = (v: string | null) => {
    const c = centavosDeMonto(v);
    return c === null ? "—" : formatCents(c);
  };
  const diferencia = centavosDeMonto(sesion.difference_ars);
  return (
    <div className="flex flex-1 items-center justify-center p-6">
      <div
        className="w-full max-w-md space-y-3 rounded-xl border border-pos-border bg-pos-surface p-6"
        data-testid="arqueo-resultado"
      >
        <h2 className="text-lg font-semibold text-pos-text">Turno cerrado</h2>
        {sesion.status === "ABANDONED" ? (
          <p className="text-sm text-pos-text-2">
            El encargado cerró este turno sin arqueo{sesion.notes ? `: ${sesion.notes}` : "."}
          </p>
        ) : (
          <dl className="grid grid-cols-2 gap-y-1 text-sm">
            <dt className="text-pos-text-2">Fondo inicial</dt>
            <dd className="text-right text-pos-text">{monto(sesion.opening_float_ars)}</dd>
            <dt className="text-pos-text-2">Esperado en el cajón</dt>
            <dd className="text-right text-pos-text" data-testid="arqueo-esperado">
              {monto(sesion.expected_cash_ars)}
            </dd>
            <dt className="text-pos-text-2">Contado</dt>
            <dd className="text-right text-pos-text">{monto(sesion.counted_cash_ars)}</dd>
            <dt className="font-semibold text-pos-text">Diferencia</dt>
            <dd
              className={`text-right font-semibold ${
                diferencia === null || diferencia === 0
                  ? "text-pos-text"
                  : diferencia > 0
                    ? "text-vk-success"
                    : "text-pos-danger"
              }`}
              data-testid="arqueo-diferencia"
            >
              {diferencia === null
                ? "—"
                : diferencia === 0
                  ? "Cierra justo"
                  : `${diferencia > 0 ? "Sobran" : "Faltan"} ${formatCents(Math.abs(diferencia))}`}
            </dd>
          </dl>
        )}
        <Button type="button" className="w-full" onClick={onListo}>
          Listo
        </Button>
      </div>
    </div>
  );
}
