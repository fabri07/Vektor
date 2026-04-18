"use client";

import { Fragment, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Trash2, RefreshCw, CheckCircle } from "lucide-react";
import {
  ingestionService,
  type UploadedFileItem,
} from "@/services/ingestion.service";

const STATUS_LABELS: Record<string, string> = {
  PENDING: "Pendiente",
  PROCESSING: "Procesando",
  NEEDS_CONFIRMATION: "Confirmar",
  DONE: "Importado",
  FAILED: "Error",
};

const STATUS_COLORS: Record<string, string> = {
  PENDING:            "text-vk-text-muted bg-vk-border-w",
  PROCESSING:         "text-vk-info bg-vk-info-bg",
  NEEDS_CONFIRMATION: "text-vk-warning bg-vk-warning-bg",
  DONE:               "text-vk-success bg-vk-success-bg",
  FAILED:             "text-vk-danger bg-vk-danger-bg",
};

function formatDate(isoString: string): string {
  return new Date(isoString).toLocaleDateString("es-AR", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  });
}

function formatType(filename: string): string {
  const ext = filename.split(".").pop()?.toUpperCase();
  return ext ?? "—";
}

function hasActiveFiles(files: UploadedFileItem[]): boolean {
  return files.some((f) =>
    f.processing_status === "PENDING" || f.processing_status === "PROCESSING",
  );
}

function ConfirmPanel({ fileId, onDone }: { fileId: string; onDone: () => void }) {
  const queryClient = useQueryClient();
  const [confirmedFields, setConfirmedFields] = useState({
    ventas: true,
    gastos: true,
    productos: true,
  });

  const { data, isLoading } = useQuery({
    queryKey: ["ingestion-preview", fileId],
    queryFn: () => ingestionService.getPreview(fileId),
    retry: false,
  });

  const confirmMutation = useMutation({
    mutationFn: () => ingestionService.confirmFile(fileId, confirmedFields),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["ingestion-files"] });
      onDone();
    },
  });

  if (isLoading) {
    return (
      <div className="ml-2 mt-2 text-xs text-vk-text-muted">Cargando datos...</div>
    );
  }

  const summary = data?.parsed_summary_json as Record<string, unknown> | null | undefined;
  const headers = Array.isArray(summary?.headers) ? (summary.headers as string[]) : null;
  const rows = Array.isArray(summary?.ventas_detectadas)
    ? (summary.ventas_detectadas as Record<string, unknown>[]).slice(0, 5)
    : null;

  return (
    <div className="ml-2 mt-3 rounded-lg border border-vk-warning/20 bg-vk-warning-bg p-4">
      <p className="mb-3 text-sm font-medium text-vk-warning">
        Datos detectados — seleccioná qué importar
      </p>

      {headers && rows && rows.length > 0 && (
        <div className="mb-3 overflow-x-auto rounded border border-vk-border-w">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-vk-border-w bg-vk-bg-light">
                {headers.slice(0, 6).map((h) => (
                  <th key={h} className="px-2 py-1 text-left font-medium text-vk-text-muted">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
                <tr key={i} className="border-b border-vk-border-w/60">
                  {headers.slice(0, 6).map((h) => (
                    <td key={h} className="px-2 py-1 text-vk-text-secondary">
                      {String(row[h] ?? "—")}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          {summary && typeof summary.rows_processed === "number" && (
            <p className="px-2 py-1 text-xs text-vk-text-muted">
              {summary.rows_processed} filas en total
            </p>
          )}
        </div>
      )}

      <div className="mb-3 flex gap-4">
        {(["ventas", "gastos", "productos"] as const).map((key) => (
          <label key={key} className="flex cursor-pointer items-center gap-1.5">
            <input
              type="checkbox"
              checked={confirmedFields[key]}
              onChange={(e) =>
                setConfirmedFields((prev) => ({ ...prev, [key]: e.target.checked }))
              }
              className="h-3.5 w-3.5 rounded border-vk-border-w accent-vk-blue"
            />
            <span className="text-xs capitalize text-vk-text-secondary">{key}</span>
          </label>
        ))}
      </div>

      {confirmMutation.isError && (
        <p className="mb-2 text-xs text-vk-danger">
          Error al confirmar. Intentá de nuevo.
        </p>
      )}

      <button
        onClick={() => confirmMutation.mutate()}
        disabled={confirmMutation.isPending || !Object.values(confirmedFields).some(Boolean)}
        className="flex items-center gap-1.5 rounded-lg bg-vk-blue px-3 py-1.5 text-xs font-medium text-white hover:bg-vk-blue-hover disabled:opacity-50 transition-colors"
      >
        <CheckCircle className="h-3.5 w-3.5" />
        {confirmMutation.isPending ? "Confirmando..." : "Confirmar datos"}
      </button>
    </div>
  );
}

export function FileListSection() {
  const queryClient = useQueryClient();
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const { data: files = [], isLoading } = useQuery<UploadedFileItem[]>({
    queryKey: ["ingestion-files"],
    queryFn: ingestionService.listFiles,
    refetchInterval: (query) => {
      const data = query.state.data as UploadedFileItem[] | undefined;
      return data && hasActiveFiles(data) ? 3_000 : 30_000;
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (fileId: string) => ingestionService.deleteFile(fileId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["ingestion-files"] });
    },
  });

  const reprocessMutation = useMutation({
    mutationFn: (fileId: string) => ingestionService.reprocessFile(fileId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["ingestion-files"] });
    },
  });

  return (
    <div className="rounded-xl border border-vk-border-w bg-vk-surface-w p-6 shadow-vk-sm">
      <h2 className="mb-4 text-sm font-semibold text-vk-text-primary">
        Archivos cargados
      </h2>

      {isLoading && (
        <div className="flex items-center gap-2 text-sm text-vk-text-muted">
          <div className="h-4 w-4 animate-spin rounded-full border-2 border-vk-border-w border-t-vk-blue" />
          Cargando...
        </div>
      )}

      {!isLoading && files.length === 0 && (
        <p className="text-sm text-vk-text-muted">No hay archivos cargados todavía.</p>
      )}

      {!isLoading && files.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-vk-border-w">
                <th className="pb-2 pr-4 text-left text-xs font-medium text-vk-text-muted">
                  Nombre
                </th>
                <th className="pb-2 pr-4 text-left text-xs font-medium text-vk-text-muted">
                  Tipo
                </th>
                <th className="pb-2 pr-4 text-left text-xs font-medium text-vk-text-muted">
                  Estado
                </th>
                <th className="pb-2 pr-4 text-left text-xs font-medium text-vk-text-muted">
                  Fecha
                </th>
                <th className="pb-2 text-left text-xs font-medium text-vk-text-muted">
                  Acciones
                </th>
              </tr>
            </thead>
            <tbody>
              {files.map((file) => (
                <Fragment key={file.id}>
                  <tr className="border-b border-vk-border-w/60">
                    <td
                      className="cursor-pointer py-2.5 pr-4 font-medium text-vk-text-primary hover:text-vk-blue"
                      onClick={() =>
                        setExpandedId((prev) => (prev === file.id ? null : file.id))
                      }
                    >
                      {file.original_filename}
                    </td>
                    <td className="py-2.5 pr-4 text-vk-text-secondary">
                      {formatType(file.original_filename)}
                    </td>
                    <td className="py-2.5 pr-4">
                      <span
                        className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${
                          STATUS_COLORS[file.processing_status] ??
                          "text-vk-text-muted bg-vk-border-w"
                        }`}
                      >
                        {STATUS_LABELS[file.processing_status] ?? file.processing_status}
                      </span>
                    </td>
                    <td className="py-2.5 pr-4 text-vk-text-secondary">
                      {formatDate(file.created_at)}
                    </td>
                    <td className="py-2.5">
                      <div className="flex items-center gap-1">
                        {/* Confirmar: abre el panel inline */}
                        {file.processing_status === "NEEDS_CONFIRMATION" && (
                          <button
                            onClick={() =>
                              setExpandedId((prev) => (prev === file.id ? null : file.id))
                            }
                            className="flex items-center gap-1 rounded px-2 py-1 text-xs font-medium text-vk-warning hover:bg-vk-warning-bg transition-colors"
                            title="Confirmar datos"
                          >
                            <CheckCircle className="h-3.5 w-3.5" />
                            Confirmar
                          </button>
                        )}
                        {/* Reintentar: PENDING o FAILED */}
                        {(file.processing_status === "PENDING" ||
                          file.processing_status === "FAILED") && (
                          <button
                            onClick={() => reprocessMutation.mutate(file.id)}
                            disabled={reprocessMutation.isPending}
                            className="flex items-center gap-1 rounded px-2 py-1 text-xs font-medium text-vk-text-secondary hover:bg-vk-bg-light transition-colors disabled:opacity-50"
                            title="Reprocesar"
                          >
                            <RefreshCw className="h-3.5 w-3.5" />
                            Reintentar
                          </button>
                        )}
                        {/* Eliminar */}
                        <button
                          onClick={() => {
                            if (confirm(`¿Eliminar "${file.original_filename}"?`)) {
                              deleteMutation.mutate(file.id);
                            }
                          }}
                          disabled={deleteMutation.isPending}
                          className="rounded p-1 text-vk-text-muted hover:text-vk-danger hover:bg-vk-danger/10 transition-colors disabled:opacity-50"
                          title="Eliminar archivo"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      </div>
                    </td>
                  </tr>

                  {expandedId === file.id &&
                    file.processing_status === "NEEDS_CONFIRMATION" && (
                      <tr>
                        <td colSpan={5} className="pb-3 pt-0">
                          <ConfirmPanel
                            fileId={file.id}
                            onDone={() => setExpandedId(null)}
                          />
                        </td>
                      </tr>
                    )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
