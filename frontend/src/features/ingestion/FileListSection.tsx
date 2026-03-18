"use client";

import { Fragment, useState } from "react";
import { useQuery } from "@tanstack/react-query";
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

function PreviewPanel({ fileId }: { fileId: string }) {
  const { data, isLoading } = useQuery({
    queryKey: ["ingestion-preview", fileId],
    queryFn: () => ingestionService.getPreview(fileId),
    retry: false,
  });

  if (isLoading) {
    return (
      <div className="ml-2 mt-1 text-xs text-vk-text-muted">
        Cargando preview...
      </div>
    );
  }

  if (!data) {
    return (
      <div className="ml-2 mt-1 text-xs text-vk-text-muted">
        El archivo aún se está procesando.
      </div>
    );
  }

  if (!data.parsed_summary_json) {
    return (
      <div className="ml-2 mt-1 text-xs text-vk-text-muted">
        Sin datos de preview disponibles.
      </div>
    );
  }

  return (
    <pre className="ml-2 mt-2 max-h-48 overflow-auto rounded bg-vk-bg-light p-3 text-xs text-vk-text-muted font-mono">
      {JSON.stringify(data.parsed_summary_json, null, 2)}
    </pre>
  );
}

export function FileListSection() {
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const { data: files = [], isLoading } = useQuery<UploadedFileItem[]>({
    queryKey: ["ingestion-files"],
    queryFn: ingestionService.listFiles,
    refetchInterval: 30_000,
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
        <p className="text-sm text-vk-text-muted">
          No hay archivos cargados todavía.
        </p>
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
                <th className="pb-2 text-left text-xs font-medium text-vk-text-muted">
                  Fecha
                </th>
              </tr>
            </thead>
            <tbody>
              {files.map((file) => (
                <Fragment key={file.id}>
                  <tr
                    onClick={() =>
                      setExpandedId((prev) =>
                        prev === file.id ? null : file.id,
                      )
                    }
                    className="cursor-pointer border-b border-vk-border-w/60 transition-colors hover:bg-vk-bg-light"
                  >
                    <td className="py-2.5 pr-4 font-medium text-vk-text-primary">
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
                        {STATUS_LABELS[file.processing_status] ??
                          file.processing_status}
                      </span>
                    </td>
                    <td className="py-2.5 text-vk-text-secondary">
                      {formatDate(file.created_at)}
                    </td>
                  </tr>

                  {expandedId === file.id && (
                    <tr>
                      <td colSpan={4} className="pb-3 pt-0">
                        <PreviewPanel fileId={file.id} />
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
