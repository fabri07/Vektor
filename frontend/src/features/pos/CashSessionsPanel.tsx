"use client";

import { useCallback, useEffect, useState } from "react";

import { Button } from "@/components/ui/Button";
import { centavosDeMonto } from "@/features/pos/useCashSession";
import { formatCents } from "@/lib/pos/cart";
import { posErrorInfo } from "@/lib/pos/errors";
import { type PosCashSession, posService } from "@/services/pos.service";

interface CashSessionsPanelProps {
  /** El turno de esta pantalla: no se ofrece cerrarlo sin arqueo desde acá. */
  turnoActual: string | null;
  terminalActual: string | null;
  onVolver: () => void;
}

/** Un turno sin fondo declarado y abierto hace más que esto se sugiere cerrar. */
const VIEJO_MS = 12 * 60 * 60 * 1000;

const monto = (v: string | null) => {
  const c = centavosDeMonto(v);
  return c === null ? "—" : formatCents(c);
};

const fecha = (iso: string) =>
  new Date(iso).toLocaleString("es-AR", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });

/**
 * Los turnos de todas las cajas, para el encargado (B8).
 *
 * Dos trabajos: **cerrar sin arqueo** los turnos que nadie va a contar —la
 * caja creaba uno en cada recarga de la pantalla, y producción tiene muchos
 * abiertos con fondo $0— y **conciliar** los cerrados, que es ver las ventas
 * que llegaron después del arqueo. Cerrar sin arqueo pide PIN (lo resuelve el
 * `PinGateModal`) y un motivo, que queda en la auditoría.
 */
export function CashSessionsPanel({ turnoActual, terminalActual, onVolver }: CashSessionsPanelProps) {
  const [turnos, setTurnos] = useState<PosCashSession[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [elegidos, setElegidos] = useState<Set<string>>(new Set());
  const [motivo, setMotivo] = useState("");
  const [cerrando, setCerrando] = useState(false);
  const [resultado, setResultado] = useState<string | null>(null);
  const [detalle, setDetalle] = useState<PosCashSession | null>(null);

  const cargar = useCallback(async () => {
    try {
      setTurnos(await posService.listCashSessions({ limit: 200 }));
      setError(null);
    } catch {
      setError("No se pudieron traer los turnos.");
    }
  }, []);

  useEffect(() => {
    void cargar();
  }, [cargar]);

  const abiertos = (turnos ?? []).filter((t) => t.status === "OPEN");
  const cerrados = (turnos ?? []).filter((t) => t.status !== "OPEN").slice(0, 30);
  const elegibles = abiertos.filter((t) => t.client_session_id !== turnoActual);
  // "Basura" = sin fondo declarado Y abierto hace más de 12 h. Los turnos que
  // la caja creaba en cada recarga nunca declararon fondo; uno recién abierto
  // en otra caja sí lo declaró (o está por declararlo, si abrió sin red), y
  // "0 ventas" no prueba nada: pueden estar en la cola offline de esa PC.
  const ahora = Date.now();
  const basura = elegibles.filter(
    (t) => !t.float_declared && ahora - new Date(t.opened_at).getTime() > VIEJO_MS,
  );

  function alternar(id: string): void {
    setElegidos((prev) => {
      const nuevo = new Set(prev);
      if (nuevo.has(id)) nuevo.delete(id);
      else nuevo.add(id);
      return nuevo;
    });
  }

  async function cerrarSinArqueo(): Promise<void> {
    const razon = motivo.trim();
    if (!razon || elegidos.size === 0) return;
    setCerrando(true);
    setResultado(null);
    let ok = 0;
    const fallas: string[] = [];
    // De a uno: cada turno se cierra con su propio request y su auditoría. La
    // ventana del PIN cubre la tanda.
    for (const id of elegidos) {
      try {
        await posService.abandonCashSession(id, razon);
        ok += 1;
      } catch (e) {
        const info = posErrorInfo(e);
        // Ya cerrado por otro camino: no es un error de esta tanda.
        if (info.code === "CASH_SESSION_ALREADY_CLOSED") ok += 1;
        else fallas.push(info.message ?? "error");
      }
    }
    setResultado(
      fallas.length
        ? `Se cerraron ${ok}. No se pudieron cerrar ${fallas.length}: ${fallas[0]}`
        : `Se cerraron ${ok} ${ok === 1 ? "turno" : "turnos"} sin arqueo.`,
    );
    setElegidos(new Set());
    setCerrando(false);
    await cargar();
  }

  async function verDetalle(id: string): Promise<void> {
    try {
      setDetalle(await posService.getCashSession(id));
    } catch {
      setError("No se pudo traer el turno.");
    }
  }

  return (
    <div className="flex flex-1 flex-col gap-4 overflow-auto p-4 text-sm" data-testid="turnos-panel">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-pos-text">Turnos de caja</h2>
        <Button type="button" variant="ghost" onClick={onVolver}>
          Volver a la caja
        </Button>
      </div>
      {error ? <p className="text-pos-danger">{error}</p> : null}
      {turnos === null && !error ? <p className="text-pos-muted">Cargando…</p> : null}

      <section className="space-y-2 rounded-xl border border-pos-border bg-pos-surface p-4">
        <div className="flex items-center justify-between">
          <h3 className="font-semibold text-pos-text">Abiertos ({abiertos.length})</h3>
          {basura.length > 0 ? (
            <button
              type="button"
              className="text-pos-link underline"
              onClick={() => setElegidos(new Set(basura.map((t) => t.id)))}
            >
              Elegir los viejos sin fondo declarado ({basura.length})
            </button>
          ) : null}
        </div>
        {abiertos.length === 0 && turnos !== null ? (
          <p className="text-pos-muted">No hay turnos abiertos.</p>
        ) : null}
        <ul className="space-y-1">
          {abiertos.map((t) => {
            const esEste = t.client_session_id === turnoActual;
            return (
              <li
                key={t.id}
                className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-pos-border p-2"
              >
                <input
                  type="checkbox"
                  aria-label={`Elegir el turno de ${t.cashier_name ?? "sin nombre"} del ${fecha(t.opened_at)}`}
                  disabled={esEste}
                  checked={elegidos.has(t.id)}
                  onChange={() => alternar(t.id)}
                />
                <span className="text-pos-text">{t.cashier_name ?? "Sin nombre"}</span>
                <span className="text-pos-muted">
                  {fecha(t.opened_at)} ·{" "}
                  {t.terminal_id === null
                    ? "navegador"
                    : t.terminal_id === terminalActual
                      ? "esta caja"
                      : "otra caja"}
                  {esEste ? " · este turno" : ""}
                </span>
                <span className="text-pos-text-2">
                  fondo {monto(t.opening_float_ars)}
                  {t.float_declared ? "" : " (sin declarar)"}
                </span>
                <span className="text-pos-text-2">
                  {t.sales_count ?? 0} {t.sales_count === 1 ? "venta" : "ventas"} · esperado{" "}
                  {monto(t.expected_cash_now_ars)}
                </span>
              </li>
            );
          })}
        </ul>
        {elegibles.length > 0 ? (
          <div className="space-y-2 border-t border-pos-border pt-3">
            <p className="text-pos-text-2">
              Cerrar sin arqueo deja el turno cerrado sin conteo: no se inventa un número. Usalo
              para turnos que nadie va a contar. Las ventas que lleguen después quedan como
              tardías. Si elegís un turno que alguien está usando, esa caja se entera y tiene que
              abrir otro.
            </p>
            <textarea
              aria-label="Motivo del cierre sin arqueo"
              placeholder="Motivo (obligatorio)"
              maxLength={500}
              value={motivo}
              onChange={(e) => setMotivo(e.target.value)}
              className="w-full rounded-lg border border-pos-border bg-pos-field p-2 text-pos-text"
            />
            <Button
              type="button"
              variant="danger"
              size="sm"
              loading={cerrando}
              disabled={cerrando || elegidos.size === 0 || motivo.trim() === ""}
              onClick={() => void cerrarSinArqueo()}
            >
              Cerrar sin arqueo ({elegidos.size})
            </Button>
          </div>
        ) : null}
        {resultado ? (
          <p className="text-pos-text" role="status">
            {resultado}
          </p>
        ) : null}
      </section>

      <section className="space-y-2 rounded-xl border border-pos-border bg-pos-surface p-4">
        <h3 className="font-semibold text-pos-text">Cerrados recientes</h3>
        {cerrados.length === 0 && turnos !== null ? (
          <p className="text-pos-muted">Todavía no hay turnos cerrados.</p>
        ) : null}
        <ul className="space-y-1">
          {cerrados.map((t) => {
            const dif = centavosDeMonto(t.difference_ars);
            return (
              <li
                key={t.id}
                className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-pos-border p-2"
              >
                <span className="text-pos-text">{t.cashier_name ?? "Sin nombre"}</span>
                <span className="text-pos-muted">
                  {fecha(t.opened_at)} → {t.closed_at ? fecha(t.closed_at) : "—"}
                </span>
                {t.status === "ABANDONED" ? (
                  <span className="text-pos-muted">sin arqueo</span>
                ) : (
                  <span className={dif === 0 ? "text-pos-text-2" : "text-pos-danger"}>
                    {dif === null
                      ? "—"
                      : dif === 0
                        ? "cerró justo"
                        : `${dif > 0 ? "sobraron" : "faltaron"} ${formatCents(Math.abs(dif))}`}
                  </span>
                )}
                <button
                  type="button"
                  className="text-pos-link underline"
                  onClick={() => void verDetalle(t.id)}
                >
                  Ver
                </button>
              </li>
            );
          })}
        </ul>
        {detalle ? (
          <div className="space-y-1 rounded-lg border border-pos-border p-3" data-testid="turno-detalle">
            <p className="text-pos-text">
              Turno de {detalle.cashier_name ?? "sin nombre"}: fondo{" "}
              {monto(detalle.opening_float_ars)}, esperado {monto(detalle.expected_cash_ars)},
              contado {monto(detalle.counted_cash_ars)}.
            </p>
            {detalle.notes ? <p className="text-pos-text-2">Notas: {detalle.notes}</p> : null}
            {detalle.late_sales.length === 0 ? (
              <p className="text-pos-muted">No llegaron ventas después del cierre.</p>
            ) : (
              <p className="text-pos-text">
                Llegaron {detalle.late_sales.length}{" "}
                {detalle.late_sales.length === 1 ? "venta" : "ventas"} después del cierre, con{" "}
                {formatCents(
                  detalle.late_sales.reduce((s, v) => s + (centavosDeMonto(v.cash_ars) ?? 0), 0),
                )}{" "}
                en efectivo que el arqueo no vio.
              </p>
            )}
          </div>
        ) : null}
      </section>
    </div>
  );
}
