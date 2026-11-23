"use client";

import { useQuery } from "@tanstack/react-query";
import { fetchVistaPrevia } from "@/services/mejorasDeDatos.service";
import type { MejoraVistaPreviaFila } from "@/types/api";

const MEDIOS: Record<string, string> = {
  cash: "Efectivo",
  transfer: "Transferencia",
  debit_card: "Tarjeta de débito",
  credit_card: "Tarjeta de crédito",
};

const ARS = new Intl.NumberFormat("es-AR", { style: "currency", currency: "ARS" });

/** «48000.00» → «$ 48.000,00». Sin número válido, el texto tal cual (no se inventa). */
function plata(valor?: string): string {
  if (valor == null) return "—";
  const n = Number(valor);
  return Number.isFinite(n) ? ARS.format(n) : valor;
}

/** ISO sin zona («2026-11-14T10:00:00») → «14/11/2026», sin pasar por `Date` (huso). */
function dia(fecha?: string): string {
  if (!fecha) return "—";
  return fecha.slice(0, 10).split("-").reverse().join("/");
}

function medio(f: MejoraVistaPreviaFila): string {
  const nombre = MEDIOS[f.medio_de_pago ?? ""] ?? f.medio_de_pago ?? "—";
  return f.medio_por_defecto ? `${nombre} (el archivo no lo decía)` : nombre;
}

/** El motivo del servidor si lo hay; si no, uno genérico. */
function motivo(e: unknown): string {
  const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  return typeof detail === "string" && detail
    ? detail
    : "No se pudo preparar la vista previa. Probá de nuevo.";
}

interface Props {
  mejoraId: string;
  aplicando: boolean;
  onAplicar: (ids: string[]) => void;
  onCerrar: () => void;
}

/**
 * La vista previa de una mejora que cambia valores: cada fila con lo que va a
 * escribir y el aviso de arqueo. «Aplicar» manda SÓLO los ids que se muestran: lo
 * que Véktor no pudo verificar queda «a mano», con su motivo, y no se toca.
 */
export function MejoraVistaPrevia({ mejoraId, aplicando, onAplicar, onCerrar }: Props) {
  const vista = useQuery({
    queryKey: ["data-improvements", "preview", mejoraId],
    queryFn: () => fetchVistaPrevia(mejoraId),
    retry: false,
    gcTime: 0, // lo que se muestra tiene que ser lo de AHORA, no una copia vieja
  });

  if (vista.isPending) {
    return <p className="text-xs text-vektor-muted">Leyendo tus archivos…</p>;
  }
  if (vista.isError) {
    return (
      <div className="space-y-1">
        <p role="alert" className="text-xs text-vektor-muted">
          {motivo(vista.error)}
        </p>
        <button
          type="button"
          onClick={onCerrar}
          className="h-8 rounded-lg border border-vektor-border px-3 text-xs text-vektor-body"
        >
          Cerrar
        </button>
      </div>
    );
  }

  const { filas, a_mano: aMano } = vista.data;
  const sinVerificar = Object.entries(aMano);

  return (
    <div className="space-y-2">
      {filas.length > 0 ? (
        <ul className="space-y-1.5" aria-label="Qué se va a cambiar">
          {filas.map((f) => (
            <li key={f.id} className="rounded-lg border border-vektor-border px-3 py-2 text-xs">
              <p className="font-medium text-vektor-white">
                {f.etiqueta}
                {f.qty != null && <span className="text-vektor-muted"> · {f.qty} u.</span>}
              </p>
              <p className="text-vektor-body">
                {f.total != null && <>Total pagado {plata(f.total)} · </>}
                {f.costo_unitario != null && <>costo por unidad {plata(f.costo_unitario)} · </>}
                {dia(f.fecha)} · {medio(f)}
              </p>
              {(f.ventas_a_costear ?? 0) > 0 && (
                <p className="text-vektor-muted">
                  Se costean {f.ventas_a_costear}{" "}
                  {f.ventas_a_costear === 1 ? "venta" : "ventas"} que estaban sin costo.
                </p>
              )}
              {f.arqueo_del_dia && (
                <p role="note" className="text-amber-400">
                  Ese día ya tiene arqueo: el cierre guardado no se modifica, pero el gasto entra
                  con su fecha real.
                </p>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-xs text-vektor-muted">No hay nada para aplicar hoy.</p>
      )}

      {sinVerificar.length > 0 && (
        <div className="text-xs text-vektor-muted">
          <p>
            {sinVerificar.length === 1
              ? "1 dato no se puede verificar y no se toca:"
              : `${sinVerificar.length} datos no se pueden verificar y no se tocan:`}
          </p>
          <ul>
            {sinVerificar.map(([quien, porque]) => (
              <li key={quien}>
                {quien}: {porque}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="flex gap-2">
        {filas.length > 0 && (
          <button
            type="button"
            disabled={aplicando}
            onClick={() => onAplicar(filas.map((f) => f.id))}
            className="h-8 rounded-lg bg-vektor-blue px-3 text-xs font-medium text-white disabled:opacity-40"
          >
            Aplicar
          </button>
        )}
        <button
          type="button"
          onClick={onCerrar}
          className="h-8 rounded-lg border border-vektor-border px-3 text-xs text-vektor-body"
        >
          Cerrar
        </button>
      </div>
    </div>
  );
}
