"use client";

import { AlertTriangle } from "lucide-react";

import type {
  PurchaseGroupItem,
  SheetPurchaseGroups,
} from "@/services/ingestion.service";

/**
 * F-H6.d — qué líneas componen cada comprobante y cómo queda repartido su envío.
 *
 * **Todo lo que se muestra acá lo calculó el servidor.** El componente no suma,
 * no divide y no redondea: si el frontend recalculara el reparto podría mostrar
 * una división distinta de la que se va a persistir, y el usuario estaría
 * aprobando una pantalla que no describe lo que pasó. Los montos llegan como
 * string decimal y sólo se les da formato de lectura (separador de miles), sin
 * volver a operar con ellos.
 *
 * Existe porque el reparto es la parte del costo que nadie puede verificar
 * después: una vez importado, el costo unitario del producto ya trae el envío
 * adentro y no queda a la vista de dónde salió. Verlo ANTES de confirmar es la
 * única oportunidad de decir "esto no es lo que quise".
 */

/**
 * Por qué un comprobante no se puede repartir, en castellano llano.
 *
 * Hoy el endpoint ya traduce los motivos del dominio antes de mandarlos, así
 * que lo normal es que este mapa no se use y el texto pase de largo. Está
 * igual, con el código crudo como clave, porque los motivos SON un set cerrado
 * del dominio (`purchase_group.py`) y quién los redacta es una decisión que
 * puede cambiar de lado: si algún día viajan como código, la pantalla no
 * empieza a mostrar `sin_identidad_de_comprobante` en la cara del usuario.
 *
 * Lo que nunca hace es tragarse el aviso: un valor que no esté en el mapa se
 * muestra tal cual. Que aparezca un texto feo es mucho menos grave que ocultar
 * que el servidor dijo que algo no se pudo repartir.
 */
const MOTIVOS: Record<string, string> = {
  sin_identidad_de_comprobante:
    "las filas no traen número de comprobante, así que no se sabe cuáles son de la misma compra",
  cifras_distintas_de_envio:
    "las filas declaran importes de envío distintos entre sí, y no se puede saber cuál es el del comprobante",
  sin_envio_compartido: "este comprobante no trae ningún envío para repartir",
};

export function explicarMotivo(codigo: string | null | undefined): string | null {
  if (!codigo) return null;
  return MOTIVOS[codigo] ?? codigo;
}

/**
 * Formato de lectura de un monto que YA vino calculado.
 *
 * Si el string no es un número (formato inesperado del servidor), se muestra tal
 * cual: inventar un `$0` sería mostrar una cifra que nadie calculó.
 */
export function pesos(valor: string): string {
  const n = Number(valor);
  if (!Number.isFinite(n)) return valor;
  return `$${n.toLocaleString("es-AR", { maximumFractionDigits: 2 })}`;
}

/** ¿Este monto es distinto de cero? Sin parsear no se puede decidir si mostrarlo. */
function esCero(valor: string): boolean {
  const n = Number(valor);
  return Number.isFinite(n) && n === 0;
}

function Comprobante({ grupo }: { grupo: PurchaseGroupItem }) {
  const motivo = explicarMotivo(grupo.motivo_no_distribuible);
  const reparto = grupo.lineas.map((l) => pesos(l.envio_asignado)).join(" / ");
  return (
    <li className="rounded border border-vk-border-w bg-vk-surface-w px-2 py-1.5">
      <p className="text-[11px] leading-snug text-vk-text-primary">
        <strong>{grupo.comprobante ?? "Sin comprobante"}</strong>
        {grupo.proveedor ? ` · ${grupo.proveedor}` : ""} · {grupo.lineas.length} línea
        {grupo.lineas.length !== 1 ? "s" : ""} · {pesos(grupo.subtotal)} de mercadería
        {!esCero(grupo.envio_compartido) && (
          <>
            {" "}
            · {pesos(grupo.envio_compartido)} de envío
            {grupo.distribuible && grupo.lineas.length > 0
              ? ` se reparten ${reparto}`
              : " no se reparten"}
          </>
        )}
      </p>
      {/* Qué le toca a cada producto. Es el número que va a quedar guardado como
          costo: sin verlo, el reparto es un porcentaje abstracto. */}
      {grupo.lineas.length > 0 && (
        <ul className="mt-1 space-y-0.5">
          {grupo.lineas.map((l) => (
            <li
              key={l.row_index}
              className="flex flex-wrap gap-x-2 pl-2 text-[10px] text-vk-text-muted"
            >
              <span className="truncate font-mono">{l.producto ?? `fila ${l.row_index + 1}`}</span>
              <span>
                {pesos(l.subtotal)} + {pesos(l.envio_asignado)} de envío ={" "}
                <strong className="text-vk-text-secondary">{pesos(l.costo_total)}</strong>
                {l.costo_unitario_final != null &&
                  ` (${pesos(l.costo_unitario_final)} por unidad)`}
              </span>
            </li>
          ))}
        </ul>
      )}
      {motivo && (
        <p className="mt-1 flex gap-1.5 text-[10px] leading-snug text-vk-warning">
          <AlertTriangle className="mt-px h-2.5 w-2.5 shrink-0" />
          <span>No se reparte: {motivo}.</span>
        </p>
      )}
      {/* Un resto sin repartir no es un detalle de redondeo que se pueda callar:
          es plata que quedó como gasto en vez de entrar al costo. */}
      {!motivo && !esCero(grupo.sin_repartir) && (
        <p className="mt-1 flex gap-1.5 text-[10px] leading-snug text-vk-warning">
          <AlertTriangle className="mt-px h-2.5 w-2.5 shrink-0" />
          <span>
            {pesos(grupo.sin_repartir)} del envío no se repartieron y quedan como gasto.
          </span>
        </p>
      )}
    </li>
  );
}

export function PurchaseGroupsPreview({
  hoja,
  className,
}: {
  hoja: SheetPurchaseGroups;
  className?: string;
}) {
  // Sin comprobantes agrupados no hay nada que mostrar, salvo que el servidor
  // haya dicho por qué no se puede repartir: ese motivo sí es información.
  const motivoHoja = explicarMotivo(hoja.motivo);
  if (hoja.grupos.length === 0 && !motivoHoja && hoja.filas_sin_comprobante === 0) {
    return null;
  }

  const truncado = hoja.grupos.length < hoja.grupos_total;

  return (
    <div className={className}>
      <p className="text-xs font-semibold text-vk-text-primary">
        Cómo quedan las compras de esta hoja
      </p>
      <p className="mb-2 text-[11px] text-vk-text-muted">
        {/* El total REAL viene aparte de la lista: decir "3 comprobantes" cuando
            hay 143 se lee como que eso es todo el archivo. */}
        {truncado
          ? `Mostrando ${hoja.grupos.length} de ${hoja.grupos_total} comprobantes.`
          : `${hoja.grupos_total} comprobante${hoja.grupos_total !== 1 ? "s" : ""}.`}{" "}
        Es lo que calculó Véktor con el mapeo actual.
      </p>

      {!hoja.puede_distribuir && motivoHoja && (
        <p className="mb-2 flex gap-1.5 rounded border border-vk-warning/20 bg-vk-warning-bg px-2 py-1.5 text-[11px] leading-snug text-vk-warning">
          <AlertTriangle className="mt-px h-3 w-3 shrink-0" />
          <span>El envío de esta hoja no se puede repartir: {motivoHoja}.</span>
        </p>
      )}

      {hoja.filas_sin_comprobante > 0 && (
        <p className="mb-2 flex gap-1.5 text-[11px] leading-snug text-vk-warning">
          <AlertTriangle className="mt-px h-3 w-3 shrink-0" />
          <span>
            {hoja.filas_sin_comprobante} fila
            {hoja.filas_sin_comprobante !== 1 ? "s" : ""} sin número de comprobante:
            no se agrupan con ninguna compra y su envío no se reparte.
          </span>
        </p>
      )}

      {hoja.grupos.length > 0 && (
        <ul className="space-y-1">
          {hoja.grupos.map((g, i) => (
            <Comprobante key={g.comprobante ?? `sin-comprobante-${i}`} grupo={g} />
          ))}
        </ul>
      )}
    </div>
  );
}
