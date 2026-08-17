"use client";

import { useEffect, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { AlertTriangle, FileUp } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { UploadSizeHint } from "@/components/ui/UploadSizeHint";
import { MAX_UPLOAD_BYTES, MAX_UPLOAD_MB } from "@/constants/upload";
import {
  customersService,
  type CustomerExtractionResponse,
  type CustomerImportPreviewItem,
  type CustomerImportPreviewResponse,
} from "@/services/customers.service";
import { useToastStore } from "@/stores/toastStore";

// Tipos que sabemos leer: foto/PDF → IA; planilla → parser determinístico.
const ALLOWED_EXTENSIONS = ".pdf,.xlsx,.xls,.csv,.png,.jpg,.jpeg,.webp,.heic,.heif";
const ALLOWED_SHEET_EXTENSIONS = ".xlsx,.xls,.csv";

type Mode = "single" | "bulk";

/**
 * Carga de clientes por archivo (Fase B). Dos modos:
 * - "single": ficha de UN cliente (foto/PDF/planilla) → se EXTRAE y se prellena el
 *   formulario de alta (human-in-the-loop, no persiste hasta que el usuario confirma).
 * - "bulk": planilla de varios clientes → preview (a crear / actualizar / inválidos /
 *   duplicados) → el usuario confirma el upsert idempotente.
 */
export function CustomerFileModal({
  isOpen,
  onClose,
  onExtracted,
  onImported,
}: {
  isOpen: boolean;
  onClose: () => void;
  onExtracted: (extraction: CustomerExtractionResponse) => void;
  onImported: () => void | Promise<void>;
}) {
  const toast = useToastStore((s) => s.add);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [mode, setMode] = useState<Mode>("single");
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<CustomerImportPreviewResponse | null>(null);

  useEffect(() => {
    if (isOpen) {
      setMode("single");
      setError(null);
      setPreview(null);
    }
  }, [isOpen]);

  // Al cambiar de modo limpiamos el preview previo (no aplica al otro flujo).
  useEffect(() => {
    setPreview(null);
    setError(null);
  }, [mode]);

  const extractMutation = useMutation({
    mutationFn: (file: File) => customersService.extractCustomer(file),
    onSuccess: (result) => {
      // La IA/parser solo sugiere: prellenamos el form y el usuario confirma.
      onExtracted(result);
    },
    onError: () =>
      toast("No se pudo leer la ficha. Cargá los datos a mano.", "error"),
  });

  const previewMutation = useMutation({
    mutationFn: (file: File) => customersService.importPreview(file),
    onSuccess: (result) => {
      setPreview(result);
      setError(null);
    },
    onError: () => toast("No se pudo leer la planilla de clientes.", "error"),
  });

  const confirmMutation = useMutation({
    mutationFn: () => {
      const rows = (preview?.items ?? [])
        .filter((it) => it.status === "create" || it.status === "update")
        .map((it) => it.customer);
      return customersService.importConfirm(rows, preview?.source_upload_id);
    },
    onSuccess: async (result) => {
      const skippedSuffix = [
        result.skipped ? `${result.skipped} salteados` : null,
        // F-I(B): fila con clave repetida dentro del archivo — va a "Otros",
        // no se fusiona sola con la fila anterior.
        result.sent_to_others
          ? `${result.sent_to_others} a revisar en Otros`
          : null,
      ]
        .filter(Boolean)
        .join(", ");
      toast(
        `Import listo: ${result.created} creados, ${result.updated} actualizados` +
          (skippedSuffix ? `, ${skippedSuffix}.` : "."),
        "success",
      );
      await onImported();
      onClose();
    },
    onError: () => toast("No se pudo confirmar el import.", "error"),
  });

  // F-N: acepta la propuesta de split para ESA fila — nunca se aplica sola.
  // Actualiza el `customer` local (lo que viaja a confirm), y limpia la
  // propuesta para que la fila deje de mostrar el hint una vez aceptada.
  function applyNameSplitSuggestion(rowIndex: number) {
    setPreview((prev) => {
      if (!prev) return prev;
      return {
        ...prev,
        items: prev.items.map((it) => {
          if (it.row_index !== rowIndex || !it.name_split_suggestion) return it;
          const { first_name, last_name } = it.name_split_suggestion;
          return {
            ...it,
            customer: { ...it.customer, name: first_name, last_name },
            name_split_suggestion: null,
          };
        }),
      };
    });
  }

  function handleFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = ""; // permite re-elegir el mismo archivo tras un fallo
    if (!file) return;
    if (file.size > MAX_UPLOAD_BYTES) {
      setError(`El archivo supera el límite de ${MAX_UPLOAD_MB} MB.`);
      return;
    }
    setError(null);
    if (mode === "single") extractMutation.mutate(file);
    else previewMutation.mutate(file);
  }

  const busy =
    extractMutation.isPending ||
    previewMutation.isPending ||
    confirmMutation.isPending;

  const confirmableCount = preview ? preview.to_create + preview.to_update : 0;

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title="Cargar clientes desde archivo"
      size="2xl"
    >
      <div className="space-y-4">
        {/* Toggle de modo */}
        <div className="flex gap-2">
          <button
            type="button"
            onClick={() => setMode("single")}
            className={`flex-1 rounded-lg border px-3 py-2 text-sm transition-colors ${
              mode === "single"
                ? "border-vk-blue bg-vk-blue/10 text-vk-text-primary"
                : "border-vk-border-w text-vk-text-secondary hover:bg-vk-bg-light"
            }`}
          >
            <span className="font-medium">Ficha individual</span>
            <span className="block text-xs text-vk-text-muted">
              Foto, PDF o planilla de un cliente
            </span>
          </button>
          <button
            type="button"
            onClick={() => setMode("bulk")}
            className={`flex-1 rounded-lg border px-3 py-2 text-sm transition-colors ${
              mode === "bulk"
                ? "border-vk-blue bg-vk-blue/10 text-vk-text-primary"
                : "border-vk-border-w text-vk-text-secondary hover:bg-vk-bg-light"
            }`}
          >
            <span className="font-medium">Lista de clientes</span>
            <span className="block text-xs text-vk-text-muted">
              Planilla con varios (XLSX/CSV)
            </span>
          </button>
        </div>

        {/* Zona de carga */}
        <div className="rounded-lg border border-vk-border-w bg-vk-surface-w px-4 py-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <p className="text-sm font-medium text-vk-text-primary">
                {mode === "single"
                  ? "Subí la ficha del cliente"
                  : "Subí la planilla de clientes"}
              </p>
              <p className="text-xs text-vk-text-muted">
                {mode === "single"
                  ? "La leemos y prellenamos el formulario. Después revisás y confirmás."
                  : "La leemos y te mostramos qué se va a crear o actualizar antes de aplicar."}
              </p>
              <UploadSizeHint className="mt-1" />
            </div>
            <input
              ref={fileInputRef}
              type="file"
              accept={mode === "single" ? ALLOWED_EXTENSIONS : ALLOWED_SHEET_EXTENSIONS}
              className="hidden"
              onChange={handleFileChange}
              disabled={busy}
            />
            <Button
              type="button"
              variant="secondary"
              size="sm"
              loading={extractMutation.isPending || previewMutation.isPending}
              disabled={busy}
              onClick={() => fileInputRef.current?.click()}
            >
              {!busy && <FileUp className="h-4 w-4" />}
              {mode === "single"
                ? "Leer ficha (foto, PDF o planilla)"
                : "Leer planilla (XLSX/CSV)"}
            </Button>
          </div>
        </div>

        {/* Preview del import masivo */}
        {mode === "bulk" && preview ? (
          <div className="space-y-3">
            <div className="grid grid-cols-5 gap-2 text-center">
              <SummaryCard label="A crear" value={preview.to_create} tone="success" />
              <SummaryCard label="A actualizar" value={preview.to_update} tone="info" />
              <SummaryCard label="A revisar" value={preview.needs_review} tone="warn" />
              <SummaryCard label="Duplicados" value={preview.duplicates} tone="warn" />
              <SummaryCard label="Inválidos" value={preview.invalid} tone="danger" />
            </div>

            {preview.warnings.length > 0 && (
              <div className="flex items-start gap-2 rounded-lg border border-vk-warning/20 bg-vk-warning-bg px-3 py-2 text-xs text-vk-warning">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <ul className="space-y-0.5">
                  {preview.warnings.map((w, i) => (
                    <li key={i}>{w}</li>
                  ))}
                </ul>
              </div>
            )}

            <div className="max-h-64 overflow-auto rounded-lg border border-vk-border-w">
              <table className="w-full text-left text-xs">
                <thead className="sticky top-0 bg-vk-surface-w text-vk-text-secondary">
                  <tr>
                    <th className="px-3 py-2 font-semibold">Fila</th>
                    <th className="px-3 py-2 font-semibold">Nombre / Razón social</th>
                    <th className="px-3 py-2 font-semibold">Documento</th>
                    <th className="px-3 py-2 font-semibold">Estado</th>
                  </tr>
                </thead>
                <tbody>
                  {preview.items.map((it) => (
                    <tr key={it.row_index} className="border-t border-vk-border-w">
                      <td className="px-3 py-1.5 text-vk-text-muted">
                        {it.row_index + 1}
                      </td>
                      <td className="px-3 py-1.5 text-vk-text-primary">
                        {it.customer.name ?? "—"}
                        <NameSplitHint item={it} onApply={applyNameSplitSuggestion} />
                      </td>
                      <td className="px-3 py-1.5 text-vk-text-secondary">
                        {it.customer.cuit ?? it.customer.dni ?? "—"}
                      </td>
                      <td className="px-3 py-1.5">
                        <StatusLabel
                          status={it.status}
                          existingName={it.existing_name}
                          issues={it.issues}
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ) : null}

        {error ? <p className="text-xs text-vk-danger">{error}</p> : null}

        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" variant="secondary" size="sm" onClick={onClose}>
            Cerrar
          </Button>
          {mode === "bulk" && preview ? (
            <Button
              type="button"
              size="sm"
              loading={confirmMutation.isPending}
              disabled={confirmableCount === 0}
              onClick={() => confirmMutation.mutate()}
            >
              {confirmableCount > 0
                ? `Importar ${confirmableCount} ${
                    confirmableCount === 1 ? "cliente" : "clientes"
                  }`
                : "Nada para importar"}
            </Button>
          ) : null}
        </div>
      </div>
    </Modal>
  );
}

function SummaryCard({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone: "success" | "info" | "warn" | "danger";
}) {
  const toneClass = {
    success: "border-vk-success/20 bg-vk-success-bg text-vk-success",
    info: "border-vk-blue/20 bg-vk-blue/10 text-vk-blue",
    warn: "border-vk-warning/20 bg-vk-warning-bg text-vk-warning",
    danger: "border-vk-danger/20 bg-vk-danger-bg text-vk-danger",
  }[tone];
  return (
    <div className={`rounded-lg border px-2 py-2 ${toneClass}`}>
      <p className="text-lg font-semibold">{value}</p>
      <p className="text-[11px] uppercase tracking-wide">{label}</p>
    </div>
  );
}

/**
 * F-N: propuesta de split nombre/apellido — nunca se aplica sola. Sólo
 * aparece para filas "create" sin `last_name` propio (el backend no la
 * calcula para el resto). `not_applicable` (razón social / una sola
 * palabra) no muestra nada — no hay nada que ofrecer.
 */
function NameSplitHint({
  item,
  onApply,
}: {
  item: CustomerImportPreviewItem;
  onApply: (rowIndex: number) => void;
}) {
  const s = item.name_split_suggestion;
  if (!s || s.status === "not_applicable") return null;
  if (s.status === "ambiguous") {
    return (
      <p className="mt-0.5 text-[11px] text-vk-text-muted" title={s.reason}>
        ¿Nombre y apellido? No está claro — revisalo si hace falta.
      </p>
    );
  }
  return (
    <p className="mt-0.5 flex flex-wrap items-center gap-1 text-[11px] text-vk-text-muted">
      <span>
        ¿Nombre: <strong>{s.first_name}</strong> / Apellido:{" "}
        <strong>{s.last_name}</strong>?
      </span>
      <button
        type="button"
        onClick={() => onApply(item.row_index)}
        className="rounded border border-vk-blue/40 px-1.5 py-0.5 text-vk-blue hover:bg-vk-blue/10"
      >
        Aplicar
      </button>
    </p>
  );
}

function StatusLabel({
  status,
  existingName,
  issues,
}: {
  status: CustomerImportPreviewResponse["items"][number]["status"];
  existingName: string | null;
  issues: string[];
}) {
  if (status === "create") return <span className="text-vk-success">Nuevo</span>;
  if (status === "update")
    return (
      <span className="text-vk-blue">
        Actualiza{existingName ? ` "${existingName}"` : ""}
      </span>
    );
  if (status === "duplicate_in_file")
    return <span className="text-vk-warning">Duplicado en el archivo</span>;
  if (status === "needs_review")
    return (
      <span className="text-vk-warning" title={issues.join(" ")}>
        Revisar{issues.length ? `: ${issues[0]}` : ""}
      </span>
    );
  return (
    <span className="text-vk-danger" title={issues.join(" ")}>
      Inválido{issues.length ? `: ${issues[0]}` : ""}
    </span>
  );
}
