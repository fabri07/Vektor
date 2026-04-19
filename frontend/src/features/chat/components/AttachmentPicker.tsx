"use client";

import { useRef } from "react";
import { Paperclip, X, Loader2, RefreshCw } from "lucide-react";
import { filesService } from "@/services/files.service";

const ALLOWED_TYPES = [
  "application/pdf",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "text/csv",
  "image/png",
  "image/jpeg",
];
const ALLOWED_EXTENSIONS = ".pdf,.xlsx,.csv,.png,.jpg,.jpeg";
const MAX_FILES = 3;
const MAX_BYTES = 10 * 1024 * 1024; // 10 MB
const ALLOWED_EXT_SET = new Set(["pdf", "xlsx", "csv", "png", "jpg", "jpeg"]);

export interface AttachmentFile {
  id: string;
  file: File;
  uploading: boolean;
  uploadedFileId?: string;
  error?: string;
}

interface AttachmentPickerProps {
  attachments: AttachmentFile[];
  onAdd: (attachment: AttachmentFile) => void;
  onUpdate: (id: string, patch: Partial<AttachmentFile>) => void;
  onRemove: (id: string) => void;
  onRetry: (id: string) => void;
  disabled?: boolean;
}

export function AttachmentPicker({
  attachments,
  onAdd,
  onUpdate,
  onRemove,
  onRetry,
  disabled,
}: AttachmentPickerProps) {
  const inputRef = useRef<HTMLInputElement>(null);

  function isAllowedFile(file: File): boolean {
    if (ALLOWED_TYPES.includes(file.type)) {
      return true;
    }

    const extension = file.name.split(".").pop()?.toLowerCase();
    return extension != null && ALLOWED_EXT_SET.has(extension);
  }

  const handleFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const availableSlots = Math.max(MAX_FILES - attachments.length, 0);
    const files = Array.from(e.target.files ?? []).slice(0, availableSlots);
    // Reset input so same file can be re-selected if needed
    e.target.value = "";

    for (const file of files) {
      if (!isAllowedFile(file)) {
        continue;
      }
      if (file.size > MAX_BYTES) {
        continue;
      }

      const id = crypto.randomUUID();
      const attachment: AttachmentFile = { id, file, uploading: true };
      onAdd(attachment);

      try {
        const uploaded = await filesService.upload(file, "chat");
        onUpdate(id, { uploading: false, uploadedFileId: uploaded.id });
      } catch {
        onUpdate(id, { uploading: false, error: "Error al subir" });
      }
    }
  };

  return (
    <div>
      {/* Chips */}
      {attachments.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-1.5">
          {attachments.map((a) => (
            <div
              key={a.id}
              className={`flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-xs font-medium
                ${a.error ? "border-vk-danger/30 bg-vk-danger-bg text-vk-danger" : "border-vk-border-w bg-vk-bg-light text-vk-text-secondary"}`}
            >
              {a.uploading ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : a.error ? (
                <button
                  type="button"
                  onClick={() => onRetry(a.id)}
                  className="rounded text-current opacity-70 hover:opacity-100 focus:outline-none"
                  aria-label={`Reintentar ${a.file.name}`}
                  title="Reintentar subida"
                >
                  <RefreshCw className="h-3 w-3" />
                </button>
              ) : (
                <Paperclip className="h-3 w-3" />
              )}
              <span className="max-w-[120px] truncate">
                {a.error ?? a.file.name}
              </span>
              <button
                type="button"
                onClick={() => onRemove(a.id)}
                className="ml-0.5 rounded text-current opacity-60 hover:opacity-100 focus:outline-none"
                aria-label={`Quitar ${a.file.name}`}
              >
                <X className="h-3 w-3" />
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Hidden file input */}
      <input
        ref={inputRef}
        type="file"
        multiple
        accept={ALLOWED_EXTENSIONS}
        className="hidden"
        onChange={(e) => void handleFileChange(e)}
        disabled={disabled || attachments.length >= MAX_FILES}
      />

      {/* Trigger button */}
      <button
        type="button"
        onClick={() => inputRef.current?.click()}
        disabled={disabled || attachments.length >= MAX_FILES}
        className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border border-vk-border-w text-vk-text-muted hover:bg-vk-bg-light hover:text-vk-text-secondary disabled:opacity-40 transition-colors"
        aria-label="Adjuntar archivo"
        title={attachments.length >= MAX_FILES ? "Máximo 3 archivos" : "Adjuntar archivo"}
      >
        <Paperclip className="h-4 w-4" />
      </button>
    </div>
  );
}
