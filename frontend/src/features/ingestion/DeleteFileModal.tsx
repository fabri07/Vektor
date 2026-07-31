"use client";

import { useQuery } from "@tanstack/react-query";
import { AlertTriangle } from "lucide-react";

import { Modal } from "@/components/ui/Modal";
import { ingestionService } from "@/services/ingestion.service";

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

  const sinDatos = Boolean(preview) && filas.length === 0;

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
