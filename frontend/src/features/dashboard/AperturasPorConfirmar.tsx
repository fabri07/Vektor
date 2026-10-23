"use client";

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  confirmarAperturaDeArchivo,
  confirmarAperturaDeProducto,
} from "@/services/dashboard.service";
import { esPinCancelado } from "@/stores/pinGateStore";
import type { AperturasPorConfirmar, ConfirmarAperturaResponse } from "@/types/api";

// "YYYY-MM-DDTHH:mm" en hora local, el formato de un input datetime-local.
function ahoraLocal(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}`
  );
}

function fechaCorta(iso: string | null): string {
  if (!iso) return "sin fecha";
  const [y, m, d] = iso.slice(0, 10).split("-");
  return `${d}/${m}/${y}`;
}

function resultadoEnTexto(r: ConfirmarAperturaResponse): string {
  const base = `Listo: ${r.productos} ${r.productos === 1 ? "producto" : "productos"} con la fecha nueva.`;
  if (r.ventas_costeadas_antes === 0) return base;
  return (
    `${base} ${r.ventas_costeadas_antes} ${r.ventas_costeadas_antes === 1 ? "venta quedó" : "ventas quedaron"} ` +
    "antes de esa fecha con el costo del stock inicial: ese costo no cambia, revisalas."
  );
}

/** El motivo del servidor (404/409 dicen qué hacer); si no hay, uno genérico. */
function mensajeDeError(e: unknown): string {
  const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === "string" && detail) return detail;
  if (detail && typeof detail === "object" && "message" in detail) {
    const m = (detail as { message?: unknown }).message;
    if (typeof m === "string" && m) return m;
  }
  return "No se pudo guardar la fecha. Probá de nuevo.";
}

function FilaConFecha({
  titulo,
  detalle,
  confirmar,
}: {
  titulo: string;
  detalle: string;
  confirmar: (fecha: string) => Promise<ConfirmarAperturaResponse>;
}) {
  const qc = useQueryClient();
  // Vacía a propósito: la fecha la dice el usuario, no se supone ninguna.
  const [fecha, setFecha] = useState("");
  const [mensaje, setMensaje] = useState<string | null>(null);
  const mutation = useMutation({
    mutationFn: () => confirmar(fecha),
    onSuccess: async (r) => {
      setMensaje(resultadoEnTexto(r));
      await qc.invalidateQueries({ queryKey: ["margins"] });
    },
    onError: (e) => {
      if (esPinCancelado(e)) return;
      setMensaje(mensajeDeError(e));
    },
  });
  const futura = fecha !== "" && fecha > ahoraLocal();

  return (
    <li className="space-y-2 rounded-lg border border-vektor-border px-3 py-2">
      <div>
        <p className="text-sm text-vektor-body">{titulo}</p>
        <p className="text-xs text-vektor-muted">{detalle}</p>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <input
          type="datetime-local"
          aria-label={`Desde cuándo tenés el stock de ${titulo}`}
          max={ahoraLocal()}
          value={fecha}
          onChange={(e) => {
            setFecha(e.target.value);
            setMensaje(null);
          }}
          className="h-8 rounded-lg border border-vektor-border bg-vektor-surface px-2 text-xs text-vektor-white"
        />
        <button
          type="button"
          disabled={fecha === "" || futura || mutation.isPending}
          onClick={() => mutation.mutate()}
          className="h-8 rounded-lg bg-vektor-blue px-3 text-xs font-medium text-white disabled:opacity-40"
        >
          Confirmar fecha
        </button>
      </div>
      {futura && <p className="text-xs text-vektor-red">La fecha no puede ser futura.</p>}
      {mensaje && <p className="text-xs text-vektor-muted">{mensaje}</p>}
    </li>
  );
}

/**
 * M·2d-1b: la fecha del stock inicial de UN archivo. Lo usan el aviso de
 * Margen y el resultado del import. El campo arranca vacío; la sugerencia (el
 * primer día registrado) sólo lo llena. Si hay filas con fecha propia, una
 * casilla sin marcar deja aceptarlas; con todas así, no hace falta otra fecha.
 */
export function ConfirmarAperturaDeArchivo({
  uploadId,
  titulo,
  detalle,
  conFechaDeFila,
  pendientes,
  fechaSugerida = null,
  onConfirmado,
}: {
  uploadId: string;
  titulo: string;
  detalle: string;
  conFechaDeFila: number;
  /** Las que no traen fecha propia: sin fecha del usuario no se confirman. */
  pendientes: number;
  fechaSugerida?: string | null;
  onConfirmado?: (r: ConfirmarAperturaResponse) => void;
}) {
  const qc = useQueryClient();
  const [fecha, setFecha] = useState("");
  const [aceptar, setAceptar] = useState(false);
  const [mensaje, setMensaje] = useState<string | null>(null);
  // Confirmado: el bloque queda de sólo lectura (reconfirmar reescribiría la
  // auditoría); para corregir, el aviso de Margen lo vuelve a ofrecer.
  const [hecho, setHecho] = useState(false);
  const mutation = useMutation({
    mutationFn: () => confirmarAperturaDeArchivo(uploadId, fecha || null, aceptar),
    onSuccess: async (r) => {
      setMensaje(resultadoEnTexto(r));
      setHecho(true);
      onConfirmado?.(r);
      await qc.invalidateQueries({ queryKey: ["margins"] });
    },
    onError: (e) => {
      if (esPinCancelado(e)) return;
      setMensaje(mensajeDeError(e));
    },
  });
  const futura = fecha !== "" && fecha > ahoraLocal();
  const alcanzaSinFecha = aceptar && pendientes === 0;
  const listo = !futura && (fecha !== "" || alcanzaSinFecha);

  return (
    <li className="space-y-2 rounded-lg border border-vektor-border px-3 py-2">
      <div>
        <p className="text-sm text-vektor-body">{titulo}</p>
        <p className="text-xs text-vektor-muted">{detalle}</p>
      </div>
      {conFechaDeFila > 0 && (
        <label className="flex items-center gap-2 text-xs text-vektor-body">
          <input
            type="checkbox"
            checked={aceptar}
            onChange={(e) => {
              setAceptar(e.target.checked);
              setMensaje(null);
            }}
          />
          {conFechaDeFila} {conFechaDeFila === 1 ? "fila trae" : "filas traen"} su propia
          fecha en el archivo: usar esa
        </label>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <input
          type="datetime-local"
          aria-label={`Desde cuándo tenés el stock de ${titulo}`}
          max={ahoraLocal()}
          value={fecha}
          onChange={(e) => {
            setFecha(e.target.value);
            setMensaje(null);
          }}
          className="h-8 rounded-lg border border-vektor-border bg-vektor-surface px-2 text-xs text-vektor-white"
        />
        {fechaSugerida && (
          <button
            type="button"
            onClick={() => {
              setFecha(`${fechaSugerida.slice(0, 10)}T00:00`);
              setMensaje(null);
            }}
            className="h-8 rounded-lg border border-vektor-border px-3 text-xs text-vektor-body"
          >
            Usar {fechaCorta(fechaSugerida)}
          </button>
        )}
        <button
          type="button"
          disabled={!listo || mutation.isPending || hecho}
          onClick={() => mutation.mutate()}
          className="h-8 rounded-lg bg-vektor-blue px-3 text-xs font-medium text-white disabled:opacity-40"
        >
          Confirmar fecha
        </button>
      </div>
      {fechaSugerida && (
        <p className="text-xs text-vektor-muted">
          {fechaCorta(fechaSugerida)} es el primer día con ventas o gastos registrados: es una
          sugerencia, confirmala sólo si ya tenías el stock ese día.
        </p>
      )}
      {!aceptar && conFechaDeFila > 0 && fecha !== "" && (
        <p className="text-xs text-vektor-muted">
          Sin marcar la casilla, esta fecha reemplaza también la de las filas que la traen.
        </p>
      )}
      {aceptar && pendientes > 0 && (
        <p className="text-xs text-vektor-muted">
          Para {pendientes === 1 ? "el producto que no trae" : `los ${pendientes} que no traen`}{" "}
          fecha, indicá desde cuándo lo tenías.
        </p>
      )}
      {futura && <p className="text-xs text-vektor-red">La fecha no puede ser futura.</p>}
      {mensaje && <p className="text-xs text-vektor-muted">{mensaje}</p>}
    </li>
  );
}

/**
 * M·2d: el stock inicial que no tiene fecha confirmada. El costo de cada venta
 * sigue a la fecha desde la que el negocio tiene ese stock: con la fecha de la
 * carga, las ventas anteriores quedan sin costo. Nada se mueve solo: el
 * usuario dice la fecha.
 */
export function AperturasPorConfirmarAviso({ datos }: { datos: AperturasPorConfirmar }) {
  const hayFilas = datos.por_archivo.length > 0 || datos.manuales.length > 0;
  return (
    <section
      aria-label="Stock inicial por confirmar"
      className="mt-4 space-y-2 rounded-xl border border-vektor-amber/40 px-4 py-3"
    >
      {datos.sin_confirmar > 0 && (
        <p className="text-sm font-medium text-vektor-white">
          Indicá desde cuándo tenías el stock inicial
        </p>
      )}
      {datos.ventas_sin_costo_antes > 0 && (
        <p className="text-xs text-vektor-amber">
          {datos.ventas_sin_costo_antes}{" "}
          {datos.ventas_sin_costo_antes === 1 ? "venta es anterior" : "ventas son anteriores"} al
          stock inicial de su producto y no tienen costo: revisá desde cuándo tenías ese stock.
        </p>
      )}
      {datos.ventas_sin_costo_con_fuente > 0 && (
        <p className="text-xs text-vektor-muted">
          {datos.ventas_sin_costo_con_fuente}{" "}
          {datos.ventas_sin_costo_con_fuente === 1 ? "venta quedó" : "ventas quedaron"} sin costo
          aunque ya hay stock inicial con costo para su fecha: quedan así hasta recostearlas.
        </p>
      )}
      {hayFilas && (
        <ul className="space-y-2">
          {datos.por_archivo.map((a) => (
            <ConfirmarAperturaDeArchivo
              key={a.upload_id}
              uploadId={a.upload_id}
              titulo={a.archivo ?? "Archivo importado"}
              detalle={`${a.productos} ${a.productos === 1 ? "producto" : "productos"} · hoy figura desde ${fechaCorta(a.fecha_actual)}`}
              conFechaDeFila={a.con_fecha_de_fila ?? 0}
              pendientes={a.aperturas - (a.con_fecha_de_fila ?? 0)}
            />
          ))}
          {datos.manuales.map((a) => (
            <FilaConFecha
              key={a.product_id}
              titulo={a.producto}
              detalle={`Cargado a mano · hoy figura desde ${fechaCorta(a.fecha_actual)}`}
              confirmar={(fecha) => confirmarAperturaDeProducto(a.product_id, fecha)}
            />
          ))}
        </ul>
      )}
    </section>
  );
}
