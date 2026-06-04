"use client";

import { Fragment, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Trash2, RefreshCw, CheckCircle } from "lucide-react";
import {
  ingestionService,
  type UploadedFileItem,
} from "@/services/ingestion.service";
import { ColumnMapperPanel } from "./ColumnMapperPanel";

const STATUS_LABELS: Record<string, string> = {
  PENDING: "Pendiente",
  PROCESSING: "Procesando",
  NEEDS_CONFIRMATION: "Confirmar",
  NEEDS_COMPLETION: "Completar datos",
  DONE: "Importado",
  FAILED: "Error",
};

const STATUS_COLORS: Record<string, string> = {
  PENDING:            "text-vk-text-muted bg-vk-border-w",
  PROCESSING:         "text-vk-info bg-vk-info-bg",
  NEEDS_CONFIRMATION: "text-vk-warning bg-vk-warning-bg",
  NEEDS_COMPLETION:   "text-vk-text-muted bg-vk-border-w",
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
                          <ColumnMapperPanel
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
