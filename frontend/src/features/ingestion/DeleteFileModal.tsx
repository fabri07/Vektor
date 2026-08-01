"use client";

import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, RotateCcw } from "lucide-react";

import { Modal } from "@/components/ui/Modal";
import { ingestionService } from "@/services/ingestion.service";

/**
 * Explicación en castellano de cada motivo de conservación.
 *
 * Espeja `REASON_LABELS` de `app/domain/file_deletion_reasons.py`. El backend
 * manda el código; acá se traduce. Un código sin entrada cae a sí mismo, así que
 * un motivo nuevo se ve raro pero nunca rompe la pantalla.
 */
const MOTIVOS: Record<string, string> = {
  otro_archivo_activo: "otro archivo activo también lo respalda",
  venta_manual_posterior: "tiene ventas posteriores",
  compra_posterior: "tiene compras posteriores",
  movimiento_posterior: "tiene movimientos de stock posteriores",
  referencia_de_otra_entidad: "otra ficha lo referencia",
  dependencias_posteriores: "tiene operaciones posteriores",
  edicion_manual_posterior: "lo editaste a mano después de importarlo",
  campo_modificado_posteriormente: "cambiaste estos campos después de importarlo",
  entidad_creada_sin_estado_anterior:
    "lo creó este archivo y no hay un valor anterior al que volver",
  sin_ledger: "se importó antes de que Véktor registrara qué creaba cada carga",
  otro_clasificado_historico_sin_procedencia:
    "salió de «Otros» y no quedó registrado de qué archivo venía",
};

interface DeleteFileModalProps {
  fileId: string | null;
  filename: string;
  isDeleting: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * Advertencia previa al borrado de un archivo.
 *
 * Borrar un archivo dejó de ser inocuo: ahora revierte las ventas, gastos,
 * movimientos de stock, filas en "Otros" y los productos que ese archivo creó.
 * El usuario tiene que ver el alcance antes de aceptar — sobre todo porque la
 * reversa alcanza TAMBIÉN a los registros que después editó a mano.
 */
export function DeleteFileModal({
  fileId,
  filename,
  isDeleting,
  onConfirm,
  onCancel,
}: DeleteFileModalProps) {
  const { data: preview, isLoading } = useQuery({
    queryKey: ["file-deletion-preview", fileId],
    queryFn: () => ingestionService.getDeletionPreview(fileId!),
    enabled: Boolean(fileId),
    // Los conteos tienen que reflejar el estado de AHORA, no uno cacheado de
    // antes de que el usuario tocara los datos.
    staleTime: 0,
  });

  const filas: Array<{ label: string; valor: number }> = preview
    ? [
        { label: "Ventas", valor: preview.ventas },
        { label: "Gastos", valor: preview.gastos },
        { label: "Productos creados por el archivo", valor: preview.productos },
        { label: "Movimientos de stock", valor: preview.movimientos_stock },
        { label: "Filas en «Otros»", valor: preview.otros },
      ].filter((f) => f.valor > 0)
    : [];

  // Productos que el archivo MODIFICÓ: no se borran, vuelven a su valor anterior.
  const aRestaurar = preview?.productos_a_restaurar ?? 0;
  // Lo que NO se va a poder revertir. El backend siempre lo supo y lo descartaba
  // en silencio; mostrarlo es lo que evita prometer una limpieza que no ocurre.
  const conservados = preview?.conservados ?? [];

  const sinDatos =
    Boolean(preview) && filas.length === 0 && aRestaurar === 0 && !conservados.length;

  return (
    <Modal
      isOpen={Boolean(fileId)}
      onClose={onCancel}
      title={`¿Eliminar "${filename}"?`}
      size="lg"
    >
      <div className="space-y-4">
        {isLoading && (
          <p className="text-sm text-vk-text-muted">Calculando qué se va a borrar…</p>
        )}

        {sinDatos && (
          <p className="text-sm text-vk-text-secondary">
            Este archivo no tiene datos importados asociados. Se elimina de la lista
            y no cambia nada en tus números.
          </p>
        )}

        {filas.length > 0 && (
          <>
            <p className="text-sm text-vk-text-secondary">
              Esto también borra los datos que este archivo cargó. Van a desaparecer
              del dashboard:
            </p>
            <ul className="divide-y divide-vk-border-w rounded-lg border border-vk-border-w">
              {filas.map((f) => (
                <li
                  key={f.label}
                  className="flex items-center justify-between px-3 py-2 text-sm"
                >
                  <span className="text-vk-text-secondary">{f.label}</span>
                  <span className="font-semibold text-vk-text-primary tabular-nums">
                    {f.valor.toLocaleString("es-AR")}
                  </span>
                </li>
              ))}
            </ul>
          </>
        )}

        {aRestaurar > 0 && (
          <p className="flex gap-2 rounded-lg border border-vk-border-w bg-vk-bg-light px-3 py-2 text-xs text-vk-text-secondary">
            <RotateCcw className="mt-px h-4 w-4 shrink-0" />
            <span>
              {aRestaurar} producto{aRestaurar !== 1 ? "s" : ""} que este archivo
              modificó vuelve{aRestaurar !== 1 ? "n" : ""} a su valor anterior. No se
              borra{aRestaurar !== 1 ? "n" : ""}: ya existían antes.
            </span>
          </p>
        )}

        {/* Lo que el borrado NO va a poder revertir. Antes se descartaba en
            silencio: el usuario veía un conteo y no se enteraba de que varias
            entidades iban a sobrevivir. */}
        {conservados.length > 0 && (
          <div className="rounded-lg border border-vk-warning/30 bg-vk-warning-bg/50 px-3 py-2">
            <p className="flex gap-2 text-xs font-medium text-vk-warning">
              <AlertTriangle className="mt-px h-4 w-4 shrink-0" />
              <span>
                {conservados.length} cosa{conservados.length !== 1 ? "s" : ""} no se
                {conservados.length !== 1 ? "n" : ""} va{conservados.length !== 1 ? "n" : ""} a
                poder revertir. Revisalas antes de continuar.
              </span>
            </p>
            <ul className="mt-1.5 space-y-1 pl-6">
              {conservados.map((c) => (
                <li key={`${c.entity_type}:${c.id}:${c.reasons[0]}`} className="text-xs">
                  <span className="font-medium text-vk-text-primary">{c.name}</span>
                  <span className="text-vk-text-muted">
                    {" — "}
                    {c.reasons.map((r) => MOTIVOS[r] ?? r).join("; ")}
                    {c.fields.length > 0 && ` (${c.fields.join(", ")})`}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {Boolean(preview?.otros_ya_clasificados) && (
          <p className="flex gap-2 rounded-lg border border-vk-border-w bg-vk-bg-light px-3 py-2 text-xs text-vk-text-muted">
            <AlertTriangle className="mt-px h-4 w-4 shrink-0" />
            <span>
              {preview!.otros_ya_clasificados} fila
              {preview!.otros_ya_clasificados !== 1 ? "s" : ""} de «Otros» que ya
              clasificaste se conserva
              {preview!.otros_ya_clasificados !== 1 ? "n" : ""}: el registro que
              generaron no se puede rastrear hasta este archivo, así que se dejan
              como están.
            </span>
          </p>
        )}

        {preview?.has_user_edits && (
          <p className="flex gap-2 rounded-lg border border-vk-warning/30 bg-vk-warning-bg/50 px-3 py-2 text-xs text-vk-warning">
            <AlertTriangle className="mt-px h-4 w-4 shrink-0" />
            <span>
              Algunos de estos registros los editaste a mano después de importarlos.
              También se van a revertir.
            </span>
          </p>
        )}

        {preview?.productos_no_rastreables && (
          <p className="flex gap-2 rounded-lg border border-vk-border-w bg-vk-bg-light px-3 py-2 text-xs text-vk-text-muted">
            <AlertTriangle className="mt-px h-4 w-4 shrink-0" />
            <span>
              Este archivo se importó antes de que Véktor registrara qué productos
              creaba cada carga, así que sus productos no se pueden identificar y
              quedan como están. Revisalos a mano en Productos si hace falta.
            </span>
          </p>
        )}

        <div className="flex justify-end gap-2 pt-1">
          <button
            type="button"
            onClick={onCancel}
            disabled={isDeleting}
            className="rounded-lg border border-vk-border-w px-3 py-1.5 text-sm text-vk-text-secondary hover:bg-vk-bg-light disabled:opacity-50 transition-colors"
          >
            Cancelar
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={isDeleting || isLoading}
            className="rounded-lg bg-vk-danger px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 disabled:opacity-50 transition-opacity"
          >
            {isDeleting ? "Eliminando…" : "Eliminar y borrar los datos"}
          </button>
        </div>
      </div>
    </Modal>
  );
}
