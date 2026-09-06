"use client";

import { useState, useRef, useCallback, useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { AxiosError } from "axios";
import { Button } from "@/components/ui/Button";
import { ingestionService } from "@/services/ingestion.service";
import { UploadSizeHint } from "@/components/ui/UploadSizeHint";
import { MAX_UPLOAD_BYTES, MAX_UPLOAD_MB } from "@/constants/upload";
import { ColumnMapperPanel } from "./ColumnMapperPanel";

const ACCEPTED_EXTENSIONS = ".xlsx,.csv,.txt,.docx,.jpg,.jpeg,.png";
const MAX_POLLS = 30;
const POLL_INTERVAL_MS = 2_000;

type Phase =
  | "idle"
  | "uploading"
  | "polling"
  | "needs_confirmation"
  | "duplicate_blocked"
  | "done"
  // Cancelado en el mapeo: no se importó NADA y el archivo quedó pendiente de
  // completar. Es un estado propio y no `done` porque `done` dice "importado
  // correctamente", que es justo lo que no pasó.
  | "canceled"
  | "failed";

const STATUS_LABELS: Record<string, string> = {
  PENDING: "Pendiente",
  PROCESSING: "Procesando...",
  NEEDS_CONFIRMATION: "Requiere confirmación",
  DONE: "Importado",
  FAILED: "Error",
};

const STATUS_COLORS: Record<string, string> = {
  PENDING:              "text-vk-text-muted bg-vk-border-w",
  PROCESSING:           "text-vk-info bg-vk-info-bg",
  NEEDS_CONFIRMATION:   "text-vk-warning bg-vk-warning-bg",
  DONE:                 "text-vk-success bg-vk-success-bg",
  FAILED:               "text-vk-danger bg-vk-danger-bg",
};

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function FileUploadSection() {
  const queryClient = useQueryClient();

  const [phase, setPhase] = useState<Phase>("idle");
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [fileId, setFileId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [uploadProgress, setUploadProgress] = useState(0);
  // C: aviso no intrusivo (re-subida por nombre) + detalle del 409 (duplicado exacto).
  const [warning, setWarning] = useState<string | null>(null);
  const [duplicateDetail, setDuplicateDetail] = useState<string | null>(null);
  const pollCount = useRef(0);
  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const stopPolling = useCallback(() => {
    if (pollTimer.current) {
      clearInterval(pollTimer.current);
      pollTimer.current = null;
    }
  }, []);

  useEffect(() => {
    return () => stopPolling();
  }, [stopPolling]);

  function startPolling(id: string) {
    pollCount.current = 0;
    stopPolling();

    pollTimer.current = setInterval(() => {
      pollCount.current += 1;

      if (pollCount.current > MAX_POLLS) {
        stopPolling();
        setPhase("failed");
        setError("El procesamiento tardó demasiado. Intentá de nuevo más tarde.");
        return;
      }

      ingestionService
        .getPreview(id)
        .then((data) => {
          if (data === null) return; // still PENDING/PROCESSING — keep polling

          if (data.processing_status === "NEEDS_CONFIRMATION") {
            stopPolling();
            // El panel de mapeo lee el preview con `useQuery(["ingestion-preview",
            // fileId])`. El polling YA lo tiene en la mano, así que se siembra en la
            // cache con la misma queryKey en vez de dejar que lo vuelva a pedir.
            queryClient.setQueryData(["ingestion-preview", id], data);
            setPhase("needs_confirmation");
            void queryClient.invalidateQueries({ queryKey: ["ingestion-files"] });
          } else if (data.processing_status === "DONE") {
            stopPolling();
            setPhase("done");
            void queryClient.invalidateQueries({ queryKey: ["ingestion-files"] });
          } else if (data.processing_status === "FAILED") {
            stopPolling();
            setPhase("failed");
            setError("El archivo no pudo procesarse.");
            void queryClient.invalidateQueries({ queryKey: ["ingestion-files"] });
          }
        })
        .catch(() => {
          stopPolling();
          setPhase("failed");
          setError("Error al verificar el estado del archivo.");
        });
    }, POLL_INTERVAL_MS);
  }

  async function handleUpload(allowDuplicate = false) {
    if (!selectedFile) return;
    setPhase("uploading");
    setError(null);
    setWarning(null);
    setDuplicateDetail(null);
    setUploadProgress(0);
    try {
      const result = await ingestionService.upload(
        selectedFile,
        "general",
        (pct) => setUploadProgress(pct),
        allowDuplicate,
      );
      setFileId(result.file_id);
      if (result.warning) setWarning(result.warning);
      setPhase("polling");
      startPolling(result.file_id);
      // Corrección C4: un archivo nuevo cambia el total para la paginación.
      void queryClient.invalidateQueries({ queryKey: ["ingestion-files-count"] });
    } catch (err) {
      const axiosErr = err as AxiosError<{ detail?: string }>;
      // C: 409 = el contenido EXACTO ya fue importado. Mostramos el detalle del
      // backend y ofrecemos "Importar igual" (reintenta con allow_duplicate=true).
      if (axiosErr.response?.status === 409) {
        setDuplicateDetail(
          axiosErr.response.data?.detail ??
            "Este archivo ya fue importado antes. Reimportarlo duplicaría los datos.",
        );
        setPhase("duplicate_blocked");
        return;
      }
      setPhase("failed");
      setError(
        "No se pudo subir el archivo. Verificá el formato (xlsx, csv, txt, docx, jpg, png) e intentá de nuevo.",
      );
    }
  }

  async function handleRetryIngestion() {
    if (!fileId) return;
    setError(null);
    pollCount.current = 0;
    setPhase("polling");
    try {
      await ingestionService.reprocessFile(fileId);
    } catch {
      setPhase("failed");
      setError("No se pudo reintentar el procesamiento. Intentá de nuevo.");
      return;
    }
    startPolling(fileId);
  }

  function handleReset() {
    stopPolling();
    setPhase("idle");
    setSelectedFile(null);
    setFileId(null);
    setError(null);
    setWarning(null);
    setDuplicateDetail(null);
    setUploadProgress(0);
    pollCount.current = 0;
  }

  function handleFileSelect(file: File) {
    if (file.size > MAX_UPLOAD_BYTES) {
      setError(`El archivo debe ser menor a ${MAX_UPLOAD_MB} MB.`);
      return;
    }
    setSelectedFile(file);
    setError(null);
  }

  function onDrop(e: React.DragEvent) {
    e.preventDefault();
    setIsDragging(false);
    const file = e.dataTransfer.files[0];
    if (file) handleFileSelect(file);
  }

  const currentStatus =
    phase === "polling"
      ? "PROCESSING"
      : phase === "needs_confirmation"
        ? "NEEDS_CONFIRMATION"
        : phase === "done"
          ? "DONE"
          : phase === "failed"
            ? "FAILED"
            : null;

  const isDropZoneVisible = phase === "idle" || phase === "uploading";

  return (
    <div className="rounded-xl border border-vk-border-w bg-vk-surface-w p-6 shadow-vk-sm">
      <h2 className="mb-4 text-sm font-semibold text-vk-text-primary">Subir archivo</h2>

      {/* Drop zone */}
      {isDropZoneVisible && (
        <div
          onDragOver={(e) => {
            e.preventDefault();
            setIsDragging(true);
          }}
          onDragLeave={() => setIsDragging(false)}
          onDrop={onDrop}
          onClick={() => phase === "idle" && inputRef.current?.click()}
          className={[
            "flex flex-col items-center justify-center rounded-lg border-2 border-dashed px-6 py-10 text-center transition-colors",
            isDragging
              ? "border-vk-blue bg-vk-blue/5 cursor-copy"
              : "border-vk-border-w cursor-pointer hover:border-vk-border-w-hover hover:bg-vk-bg-light",
          ].join(" ")}
        >
          <input
            ref={inputRef}
            type="file"
            accept={ACCEPTED_EXTENSIONS}
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) handleFileSelect(f);
              e.target.value = "";
            }}
          />
          <svg
            className="h-8 w-8 text-vk-text-muted"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={1.5}
              d="M9 13h6m-3-3v6m5 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"
            />
          </svg>

          {selectedFile ? (
            <>
              <p className="mt-3 text-sm font-medium text-vk-text-primary">
                {selectedFile.name}
              </p>
              <p className="mt-1 text-xs text-vk-text-muted">
                {formatBytes(selectedFile.size)}
              </p>
            </>
          ) : (
            <>
              <p className="mt-3 text-sm text-vk-text-secondary">
                Arrastrá un archivo o{" "}
                <span className="text-vk-blue">hacé click para buscar</span>
              </p>
              <p className="mt-1 text-xs text-vk-text-muted">
                xlsx, csv, txt, docx, jpg, png
              </p>
              <UploadSizeHint className="mt-1" />
            </>
          )}
        </div>
      )}

      {/* Upload progress bar */}
      {phase === "uploading" && (
        <div className="mt-3 space-y-1">
          <div className="h-1.5 w-full overflow-hidden rounded-full bg-vk-border-w">
            <div
              className="h-full rounded-full bg-vk-blue transition-all duration-200"
              style={{ width: `${uploadProgress}%` }}
            />
          </div>
          <p className="text-right text-xs text-vk-text-muted">{uploadProgress}%</p>
        </div>
      )}

      {/* Status badge when processing/done/failed */}
      {currentStatus && (
        <div className="mt-4 flex items-center gap-3">
          {currentStatus === "PROCESSING" && (
            <div className="h-4 w-4 animate-spin rounded-full border-2 border-vk-border-w border-t-vk-blue" />
          )}
          <span
            className={`rounded-full px-3 py-1 text-xs font-medium ${
              STATUS_COLORS[currentStatus] ?? "text-vk-text-muted bg-vk-border-w"
            }`}
          >
            {STATUS_LABELS[currentStatus] ?? currentStatus}
          </span>
          {selectedFile && phase !== "done" && (
            <span className="text-xs text-vk-text-muted">{selectedFile.name}</span>
          )}
        </div>
      )}

      {/* Error message */}
      {error && (
        <p className="mt-3 rounded-lg border border-vk-danger/20 bg-vk-danger-bg px-4 py-2.5 text-sm text-vk-danger">
          {error}
        </p>
      )}

      {/* C: aviso no intrusivo — re-subida por nombre (versión actualizada). */}
      {warning && (
        <p className="mt-3 rounded-lg border border-vk-warning/20 bg-vk-warning-bg px-4 py-2.5 text-sm text-vk-warning">
          {warning}
        </p>
      )}

      {/* C: contenido EXACTO ya importado → 409. Detalle del backend + "Importar igual". */}
      {phase === "duplicate_blocked" && (
        <div className="mt-4 rounded-lg border border-vk-warning/30 bg-vk-warning-bg p-4">
          <p className="mb-3 text-sm text-vk-warning">
            {duplicateDetail}
          </p>
          <div className="flex gap-2">
            <Button size="sm" onClick={() => void handleUpload(true)}>
              Importar igual
            </Button>
            <Button size="sm" variant="secondary" onClick={handleReset}>
              Cancelar
            </Button>
          </div>
        </div>
      )}

      {/* Revisión del mapeo: el MISMO panel que se abre desde "Archivos cargados".
          Antes acá había un confirm propio que mandaba `column_mappings: []`, así que
          el backend caía a la heurística de encabezados y columnas como
          "Especificaciones" (→ descripción) o "Tienda" (→ proveedor) no se mapeaban
          NUNCA por este camino. Era la tercera implementación del confirm en el
          frontend; ahora hay dos, las dos adentro de este panel. */}
      {phase === "needs_confirmation" && fileId && (
        <div className="mt-4">
          <ColumnMapperPanel
            fileId={fileId}
            onDone={() => setPhase("done")}
            onCancel={() => setPhase("canceled")}
          />
        </div>
      )}

      {/* Done — SOLO tras un confirm exitoso. */}
      {phase === "done" && (
        <div className="mt-4 rounded-lg border border-vk-success/20 bg-vk-success-bg px-4 py-3 text-sm text-vk-success">
          ✓ Archivo importado correctamente.
        </div>
      )}

      {/* Cancelado: neutral, nunca verde. El archivo sigue existiendo y se puede
          retomar desde "Archivos cargados" (el backend lo dejó en
          NEEDS_COMPLETION), así que lo que corresponde decir es dónde quedó. */}
      {phase === "canceled" && (
        <div className="mt-4 rounded-lg border border-vk-border-w bg-vk-bg-light px-4 py-3 text-sm text-vk-text-secondary">
          Importación cancelada. No se importó ningún dato: el archivo quedó
          pendiente en «Archivos cargados» y podés retomar el mapeo desde ahí.
        </div>
      )}

      {/* Actions */}
      <div className="mt-4 flex gap-2">
        {phase === "idle" && selectedFile && (
          <>
            <Button size="sm" onClick={() => void handleUpload()}>
              Subir archivo
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => setSelectedFile(null)}
            >
              Quitar
            </Button>
          </>
        )}
        {phase === "uploading" && (
          <Button size="sm" loading>
            Subiendo...
          </Button>
        )}
        {phase === "failed" && fileId && (
          <Button size="sm" onClick={() => void handleRetryIngestion()}>
            Reintentar
          </Button>
        )}
        {(phase === "done" || phase === "canceled" || phase === "failed") && (
          <Button size="sm" variant="secondary" onClick={handleReset}>
            Subir otro archivo
          </Button>
        )}
      </div>
    </div>
  );
}
