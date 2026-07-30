"use client";

import { Fragment, useEffect, useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Trash2, RefreshCw, CheckCircle, History } from "lucide-react";
import {
  ingestionService,
  type UploadedFileItem,
  type RereadPreviewResponse,
  type RereadApplyResponse,
  type RereadNotRevertedEntity,
} from "@/services/ingestion.service";
import { useToastStore } from "@/stores/toastStore";
import { Modal } from "@/components/ui/Modal";
import { TableSearch } from "@/components/ui/TableSearch";
import { matchesRow } from "@/lib/search";
import { ColumnMapperPanel } from "./ColumnMapperPanel";
import { RereadDiff } from "./RereadDiff";
import { RereadProgress } from "./RereadProgress";
import { IndeterminateBar } from "./IndeterminateBar";

// F9b: rótulo legible por `kind` para el aviso de "no revertido". Sin fallback
// silencioso a un texto genérico engañoso — si el backend agrega un `kind`
// nuevo, se muestra tal cual (ver uso: `ENTITY_KIND_LABEL[entity.kind] ?? entity.kind`).
const ENTITY_KIND_LABEL: Record<string, string> = {
  customer: "el cliente",
  supplier: "el proveedor",
  product: "el producto",
};

// F9b: solo cliente/proveedor tienen ficha propia navegable (`/customers/[id]`,
// `/suppliers/[id]`); productos no la tienen todavía → sin link para ese kind.
const ENTITY_DETAIL_PATH: Record<string, string> = {
  customer: "/customers",
  supplier: "/suppliers",
};

const STATUS_LABELS: Record<string, string> = {
  PENDING: "Pendiente",
  PROCESSING: "Procesando",
  IMPORTING: "Importando…",
  NEEDS_CONFIRMATION: "Confirmar",
  NEEDS_COMPLETION: "Completar datos",
  DONE: "Importado",
  FAILED: "Error",
};

const STATUS_COLORS: Record<string, string> = {
  PENDING:            "text-vk-text-muted bg-vk-border-w",
  PROCESSING:         "text-vk-info bg-vk-info-bg",
  IMPORTING:          "text-vk-info bg-vk-info-bg",
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
    f.processing_status === "PENDING" ||
    f.processing_status === "PROCESSING" ||
    // IMPORTING: el confirm está corriendo → seguir el poll rápido (3s) hasta que
    // el archivo pase a DONE/NEEDS_COMPLETION.
    f.processing_status === "IMPORTING",
  );
}


/** Fase de la relectura dentro del modal. */
type RereadPhase = "loading" | "preview" | "applying" | "result";

interface RereadState {
  fileId: string;
  filename: string;
  phase: RereadPhase;
  preview: RereadPreviewResponse | null;
  result: RereadApplyResponse | null;
  // run del apply en background (fase "applying") para hacer polling del estado.
  runId?: string;
}

export function FileListSection() {
  const queryClient = useQueryClient();
  const addToast = useToastStore((s) => s.add);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [search, setSearch] = useState("");

  // Estado del modal de relectura (en memoria de sesión).
  const [reread, setReread] = useState<RereadState | null>(null);
  // B: confirmación previa a arrancar la relectura (borra los datos cargados del
  // archivo y lo vuelve a leer desde cero).
  const [rereadConfirm, setRereadConfirm] = useState<UploadedFileItem | null>(
    null,
  );
  // Resultado de la última relectura aplicada por archivo → alimenta el badge
  // "Relectura" y permite re-abrir el diff sin volver a llamar al backend.
  const [rereadResults, setRereadResults] = useState<
    Record<string, RereadApplyResponse>
  >({});
  // F9b: confirmación previa a DESHACER una relectura ya aplicada (distinta de
  // `rereadConfirm`, que confirma el ARRANQUE de la relectura). Bool simple:
  // mientras está en true, oculta el modal de resultado y muestra este en su
  // lugar (mismo patrón secuencial que `rereadConfirm` → `reread`, nunca dos
  // modales apilados a la vez).
  const [undoConfirmOpen, setUndoConfirmOpen] = useState(false);
  // F9b: aviso persistente (no un toast que desaparece a los 4s) con los
  // clientes/proveedores/productos que el último undo NO pudo revertir porque
  // alguien los editó después de la relectura. Sobrevive al cierre del modal
  // de resultado (el undo lo cierra) — el usuario lo descarta a mano.
  const [notRevertedNotice, setNotRevertedNotice] = useState<{
    filename: string;
    entities: RereadNotRevertedEntity[];
  } | null>(null);

  const { data: files = [], isLoading } = useQuery<UploadedFileItem[]>({
    queryKey: ["ingestion-files"],
    queryFn: ingestionService.listFiles,
    refetchInterval: (query) => {
      const data = query.state.data as UploadedFileItem[] | undefined;
      return data && hasActiveFiles(data) ? 3_000 : 30_000;
    },
  });

  // Filtra por los mismos valores que muestran las celdas de la tabla:
  // nombre, tipo, etiqueta de estado (en español) y fecha formateada.
  const filteredFiles = useMemo(
    () =>
      files.filter((f) =>
        matchesRow(
          [
            f.original_filename,
            formatType(f.original_filename),
            STATUS_LABELS[f.processing_status] ?? f.processing_status,
            formatDate(f.created_at),
          ],
          search,
        ),
      ),
    [files, search],
  );

  // Invalida la lista de archivos + las queries de datos que una relectura
  // puede haber modificado (ventas/gastos/productos/inventario/scores/otros).
  // Las claves deben coincidir con las que usan las pantallas reales: el match
  // de TanStack Query es por prefijo, así que ["sales-entries"] cubre
  // ["sales-entries", from, to], pero ["sales-all"]/["sales-date-range"] son
  // claves distintas y hay que invalidarlas aparte.
  function invalidateDataQueries() {
    const keys: string[] = [
      "ingestion-files",
      // ventas
      "sales-entries",
      "sales-all",
      "sales-date-range",
      "sales-by-customer",
      // gastos
      "expenses-entries",
      "expenses-all",
      "expenses-date-range",
      // productos / inventario
      "products",
      "products-list",
      "products-all",
      "product-categories",
      "inventory",
      // sin clasificar (una relectura puede mover filas a/desde "Otros")
      "others-pending",
      "others-pending-count",
      // análisis / dashboard
      "breakdown",
      "forecast",
      "health-scores",
    ];
    for (const key of keys) {
      void queryClient.invalidateQueries({ queryKey: [key] });
    }
  }

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

  const rereadPreviewMutation = useMutation({
    mutationFn: (file: UploadedFileItem) =>
      ingestionService.rereadPreview(file.id),
    // Abrir el modal con la barra de progreso ANTES de esperar la respuesta.
    onMutate: (file) => {
      setReread({
        fileId: file.id,
        filename: file.original_filename,
        phase: "loading",
        preview: null,
        result: rereadResults[file.id] ?? null,
      });
    },
    onSuccess: (preview, file) => {
      setReread((prev) =>
        prev && prev.fileId === file.id
          ? { ...prev, phase: "preview", preview }
          : prev,
      );
    },
    onError: () => {
      setReread(null);
      addToast("No se pudo previsualizar la relectura.", "error");
    },
  });

  // El apply corre en BACKGROUND: el POST encola y devuelve run_id; pasamos a fase
  // "applying" y hacemos polling del estado (ver rereadStatusQuery + su useEffect).
  const rereadApplyMutation = useMutation({
    mutationFn: (fileId: string) => ingestionService.rereadApply(fileId),
    onMutate: (fileId) => {
      setReread((prev) =>
        prev && prev.fileId === fileId ? { ...prev, phase: "applying" } : prev,
      );
    },
    onSuccess: (start) => {
      setReread((prev) =>
        prev && prev.fileId === start.file_id
          ? { ...prev, phase: "applying", runId: start.run_id }
          : prev,
      );
    },
    onError: (_err, fileId) => {
      // Volver a la fase preview para que el usuario pueda reintentar.
      setReread((prev) =>
        prev && prev.fileId === fileId ? { ...prev, phase: "preview" } : prev,
      );
      addToast("No se pudo iniciar la relectura.", "error");
    },
  });

  // Polling del apply en background mientras la fase es "applying".
  const applyingRunId = reread?.phase === "applying" ? reread.runId : undefined;
  const applyingFileId = reread?.phase === "applying" ? reread.fileId : undefined;
  const rereadStatusQuery = useQuery({
    queryKey: ["reread-run", applyingFileId, applyingRunId],
    queryFn: () =>
      ingestionService.rereadRunStatus(applyingFileId!, applyingRunId!),
    enabled: Boolean(applyingFileId && applyingRunId),
    refetchInterval: (query) => {
      const s = query.state.data?.status;
      return s === "APPLIED" || s === "FAILED" ? false : 2_500;
    },
  });

  useEffect(() => {
    const data = rereadStatusQuery.data;
    if (!data) return;
    if (data.status === "APPLIED") {
      const result: RereadApplyResponse = {
        file_id: data.file_id,
        run_id: data.run_id,
        to_update: data.to_update,
        preserved: data.preserved,
        new: data.new,
        voided: data.voided,
        inserted: data.inserted,
        legacy_fallback: data.legacy_fallback,
        items: data.items,
      };
      setRereadResults((prev) => ({ ...prev, [result.file_id]: result }));
      setReread((prev) =>
        prev && prev.fileId === result.file_id
          ? { ...prev, phase: "result", result, runId: undefined }
          : prev,
      );
      invalidateDataQueries();
      addToast("Relectura aplicada.", "success");
    } else if (data.status === "FAILED") {
      setReread((prev) =>
        prev && prev.fileId === data.file_id
          ? { ...prev, phase: "preview", runId: undefined }
          : prev,
      );
      addToast(data.error || "No se pudo aplicar la relectura.", "error");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rereadStatusQuery.data]);

  const rereadUndoMutation = useMutation({
    mutationFn: (fileId: string) => ingestionService.rereadUndo(fileId),
    onSuccess: (res, fileId) => {
      const filename =
        reread && reread.fileId === fileId ? reread.filename : fileId;
      setRereadResults((prev) => {
        const next = { ...prev };
        delete next[fileId];
        return next;
      });
      setReread(null);
      invalidateDataQueries();
      addToast("Relectura deshecha.", "success");
      // F9b: si el backend no pudo revertir algún cliente/proveedor/producto
      // (porque alguien lo editó después de la relectura), avisarlo aparte —
      // no alcanza con el toast de éxito, que ya desapareció para cuando el
      // usuario necesita leerlo con calma.
      if (res.not_reverted_entities.length > 0) {
        setNotRevertedNotice({ filename, entities: res.not_reverted_entities });
      }
    },
    onError: () => {
      addToast("No se pudo deshacer la relectura.", "error");
    },
  });

  function openRereadBadge(file: UploadedFileItem) {
    const result = rereadResults[file.id];
    if (!result) return;
    setReread({
      fileId: file.id,
      filename: file.original_filename,
      phase: "result",
      preview: null,
      result,
    });
  }

  return (
    <div className="rounded-xl border border-vk-border-w bg-vk-surface-w p-6 shadow-vk-sm">
      <h2 className="mb-4 text-sm font-semibold text-vk-text-primary">
        Archivos cargados
      </h2>

      {/* F9b: aviso de entidades que el último "deshacer" NO pudo revertir
          (editadas a mano después de la relectura). Persiste hasta que el
          usuario lo cierra — no es un toast que se pierde solo. */}
      {notRevertedNotice && (
        <div
          role="status"
          className="mb-4 rounded-lg border border-vk-warning/40 bg-vk-warning-bg px-4 py-3 text-xs text-vk-warning"
        >
          <div className="mb-1.5 flex items-start justify-between gap-2">
            <p className="font-semibold">
              Se deshizo la relectura de &quot;{notRevertedNotice.filename}&quot;,
              pero algunos registros no se pudieron revertir:
            </p>
            <button
              type="button"
              onClick={() => setNotRevertedNotice(null)}
              className="flex-shrink-0 rounded p-0.5 text-vk-warning/70 hover:text-vk-warning"
              aria-label="Cerrar aviso"
            >
              <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
          <ul className="list-disc space-y-1 pl-4">
            {notRevertedNotice.entities.map((entity) => (
              <li key={`${entity.kind}-${entity.id}`}>
                No se revirtió {ENTITY_KIND_LABEL[entity.kind] ?? entity.kind} —
                fue editado manualmente después de la relectura.
                {ENTITY_DETAIL_PATH[entity.kind] && (
                  <>
                    {" "}
                    <a
                      href={`${ENTITY_DETAIL_PATH[entity.kind]}/${entity.id}`}
                      className="underline hover:no-underline"
                    >
                      Ver ficha
                    </a>
                  </>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

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
        <TableSearch
          value={search}
          onChange={setSearch}
          placeholder="Buscar archivo…"
          className="mb-3 w-full sm:max-w-xs"
        />
      )}

      {!isLoading && files.length > 0 &&
        (filteredFiles.length === 0 ? (
          <p className="text-sm text-vk-text-muted">
            No hay archivos que coincidan con la búsqueda.
          </p>
        ) : (
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
              {filteredFiles.map((file) => (
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
                      <div className="flex items-center gap-1.5">
                        <span
                          className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${
                            STATUS_COLORS[file.processing_status] ??
                            "text-vk-text-muted bg-vk-border-w"
                          }`}
                        >
                          {STATUS_LABELS[file.processing_status] ?? file.processing_status}
                        </span>
                        {/* Señal "Relectura": aparece si el archivo tuvo una
                            relectura aplicada en esta sesión. Click → re-abre el diff. */}
                        {rereadResults[file.id] && (
                          <button
                            type="button"
                            onClick={() => openRereadBadge(file)}
                            className="flex items-center gap-1 rounded-full bg-vk-info-bg px-2 py-0.5 text-xs font-medium text-vk-info transition-colors hover:brightness-110"
                            title="Ver cambios de la relectura"
                          >
                            <History className="h-3 w-3" />
                            Relectura
                          </button>
                        )}
                      </div>
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
                        {/* Reintentar: PENDING, FAILED o PROCESSING trabado (>5 min:
                            el worker murió sin escribir FAILED). El archivo ya está
                            en R2 → se re-lee sin re-subir. */}
                        {(file.processing_status === "PENDING" ||
                          file.processing_status === "FAILED" ||
                          (file.processing_status === "PROCESSING" &&
                            Date.now() - new Date(file.created_at).getTime() > 5 * 60 * 1000)) && (
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
                        {/* Volver a leer: re-lee el archivo ya subido (incluso DONE)
                            sin re-subir. Distinto de "Reintentar" (PENDING/FAILED). */}
                        {(file.processing_status === "DONE" ||
                          file.processing_status === "NEEDS_COMPLETION") && (
                          <button
                            onClick={() => setRereadConfirm(file)}
                            disabled={
                              rereadPreviewMutation.isPending &&
                              rereadPreviewMutation.variables?.id === file.id
                            }
                            className="flex items-center gap-1 rounded px-2 py-1 text-xs font-medium text-vk-info hover:bg-vk-info-bg transition-colors disabled:opacity-50"
                            title="Volver a leer este archivo"
                          >
                            <RefreshCw className="h-3.5 w-3.5" />
                            Volver a leer
                          </button>
                        )}
                        {/* Eliminar: oculto mientras el import corre (IMPORTING);
                            el backend además rechaza el DELETE con 409. */}
                        {file.processing_status !== "IMPORTING" && (
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
                        )}
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

                  {/* IMPORTING: el confirm está corriendo. Barra indeterminada
                      honesta (sin %) + aviso de no cerrar. */}
                  {file.processing_status === "IMPORTING" && (
                    <tr>
                      <td colSpan={5} className="pb-3 pt-0">
                        <div className="flex flex-col gap-2 rounded-lg border border-vk-info/30 bg-vk-info-bg/40 px-3 py-2.5">
                          <IndeterminateBar />
                          <p className="text-xs text-vk-info">
                            Importando… no cierres esta ventana mientras termina.
                          </p>
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
        ))}

      {/* B: confirmación antes de arrancar la relectura. */}
      <Modal
        isOpen={rereadConfirm !== null}
        onClose={() => setRereadConfirm(null)}
        title="Volver a leer archivo"
        size="md"
      >
        {rereadConfirm && (
          <div className="flex flex-col gap-4">
            <p className="text-sm text-vektor-body">
              Se van a{" "}
              <span className="font-semibold text-vektor-white">
                borrar los datos cargados
              </span>{" "}
              de{" "}
              <span className="font-medium text-vektor-white">
                {rereadConfirm.original_filename}
              </span>{" "}
              y volver a leerlo desde cero. ¿Confirmás?
            </p>
            <div className="flex items-center justify-end gap-2">
              <button
                type="button"
                onClick={() => setRereadConfirm(null)}
                className="rounded-lg px-3 py-1.5 text-sm font-medium text-vektor-body hover:bg-vektor-surface transition-colors"
              >
                Cancelar
              </button>
              <button
                type="button"
                onClick={() => {
                  rereadPreviewMutation.mutate(rereadConfirm);
                  setRereadConfirm(null);
                }}
                className="rounded-lg bg-vk-blue px-3 py-1.5 text-sm font-medium text-white hover:brightness-110 transition-colors"
              >
                Sí, releer
              </button>
            </div>
          </div>
        )}
      </Modal>

      <Modal
        isOpen={reread !== null && !undoConfirmOpen}
        onClose={() => setReread(null)}
        title="Relectura de archivo"
        size="2xl"
      >
        {reread && (
          <div className="flex flex-col gap-4">
            <p className="text-sm text-vektor-muted">
              <span className="font-medium text-vektor-white">
                {reread.filename}
              </span>
            </p>

            {/* Fase de carga: leyendo + estimando los cambios */}
            {reread.phase === "loading" && (
              <RereadProgress />
            )}

            {/* Fase aplicando: escribiendo los cambios */}
            {reread.phase === "applying" && (
              <>
                <RereadProgress label="Aplicando cambios…" />
                <p className="text-xs text-vektor-muted">
                  Se está aplicando en segundo plano. En archivos grandes puede
                  tardar unos minutos; podés esperar acá o cerrar y volver — se
                  sigue procesando igual.
                </p>
              </>
            )}

            {/* Fase preview: contadores + nota legacy + acciones */}
            {reread.phase === "preview" && reread.preview && (
              <>
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                  <CountCard
                    label="A actualizar"
                    value={reread.preview.counts.to_update}
                    tone="info"
                  />
                  <CountCard
                    label="Preservados"
                    value={reread.preview.counts.preserved}
                    tone="muted"
                  />
                  <CountCard
                    label="Nuevos"
                    value={reread.preview.counts.new}
                    tone="success"
                  />
                  <CountCard
                    label="A anular"
                    value={reread.preview.counts.to_void}
                    tone="danger"
                  />
                </div>

                {/* Impacto en productos + filas ya importadas (sin cambios) */}
                {(reread.preview.counts.products_new > 0 ||
                  reread.preview.counts.products_restock > 0 ||
                  reread.preview.counts.unchanged > 0) && (
                  <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
                    <CountCard
                      label="Productos nuevos"
                      value={reread.preview.counts.products_new}
                      tone="success"
                    />
                    <CountCard
                      label="Reposición de stock"
                      value={reread.preview.counts.products_restock}
                      tone="info"
                    />
                    <CountCard
                      label="Sin cambios"
                      value={reread.preview.counts.unchanged}
                      tone="muted"
                    />
                  </div>
                )}

                {reread.preview.counts.unchanged > 0 && (
                  <div className="rounded-lg border border-vektor-border bg-vektor-surface px-3 py-2 text-xs text-vektor-muted">
                    {reread.preview.counts.unchanged} fila(s) ya estaban importadas
                    de este archivo: la relectura las saltea (no se duplican).
                  </div>
                )}

                {reread.preview.legacy_fallback && (
                  <div className="rounded-lg border border-vk-warning/40 bg-vk-warning-bg px-3 py-2 text-xs text-vk-warning">
                    Primera relectura: reconstrucción best-effort. No hay un import
                    previo trazable, así que los cambios se calculan lo mejor posible.
                  </div>
                )}

                {reread.preview.sample_changes.length > 0 && (
                  <div>
                    <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-vektor-muted">
                      Vista previa de cambios (antes / después)
                    </h3>
                    <RereadDiff items={reread.preview.sample_changes} />
                    <p className="mt-1.5 text-[11px] text-vektor-muted">
                      Muestra una parte de los cambios. Los contadores son una
                      estimación; al aplicar verás el resultado exacto y podrás
                      deshacerlo.
                    </p>
                  </div>
                )}

                <p className="text-xs text-vektor-muted">
                  Volvemos a leer el archivo ya subido y aplicamos las correcciones
                  sin que tengas que volver a subirlo. Podés deshacerlo después.
                </p>

                <div className="flex items-center justify-end gap-2">
                  <button
                    type="button"
                    onClick={() => setReread(null)}
                    className="rounded-lg px-3 py-1.5 text-sm font-medium text-vektor-body hover:bg-vektor-surface transition-colors"
                  >
                    Cancelar
                  </button>
                  <button
                    type="button"
                    onClick={() => rereadApplyMutation.mutate(reread.fileId)}
                    disabled={rereadApplyMutation.isPending}
                    className="flex items-center gap-1.5 rounded-lg bg-vk-blue px-3 py-1.5 text-sm font-medium text-white hover:brightness-110 transition-colors disabled:opacity-50"
                  >
                    Aplicar relectura
                  </button>
                </div>
              </>
            )}

            {/* Fase resultado: diff antes/después + deshacer */}
            {reread.phase === "result" && reread.result && (
              <>
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                  <CountCard
                    label="Actualizados"
                    value={reread.result.to_update}
                    tone="info"
                  />
                  <CountCard
                    label="Preservados"
                    value={reread.result.preserved}
                    tone="muted"
                  />
                  <CountCard
                    label="Insertados"
                    value={reread.result.inserted}
                    tone="success"
                  />
                  <CountCard
                    label="Anulados"
                    value={reread.result.voided}
                    tone="danger"
                  />
                </div>

                <div>
                  <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-vektor-muted">
                    Cambios (antes / después)
                  </h3>
                  <RereadDiff items={reread.result.items} />
                </div>

                <div className="flex items-center justify-between gap-2 border-t border-vk-border-w pt-3">
                  <button
                    type="button"
                    onClick={() => setUndoConfirmOpen(true)}
                    disabled={rereadUndoMutation.isPending}
                    className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium text-vk-danger hover:bg-vk-danger/10 transition-colors disabled:opacity-50"
                  >
                    {rereadUndoMutation.isPending && (
                      <div className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-vk-danger/40 border-t-vk-danger" />
                    )}
                    Deshacer relectura
                  </button>
                  <button
                    type="button"
                    onClick={() => setReread(null)}
                    className="rounded-lg px-3 py-1.5 text-sm font-medium text-vektor-body hover:bg-vektor-surface transition-colors"
                  >
                    Cerrar
                  </button>
                </div>
              </>
            )}
          </div>
        )}
      </Modal>

      {/* F9b: confirmación previa a deshacer una relectura ya aplicada.
          Mismo patrón secuencial que el Modal `rereadConfirm` de arriba (nunca
          dos modales abiertos a la vez): mientras este está abierto, el modal
          de resultado de arriba se oculta (ver `isOpen={reread !== null &&
          !undoConfirmOpen}`) y reaparece si el usuario cancela. */}
      <Modal
        isOpen={undoConfirmOpen}
        onClose={() => setUndoConfirmOpen(false)}
        title="Deshacer relectura"
        size="md"
      >
        {reread && (
          <div className="flex flex-col gap-4">
            <p className="text-sm text-vektor-body">
              ¿Deshacer la relectura de{" "}
              <span className="font-medium text-vektor-white">
                {reread.filename}
              </span>
              ? Se revierten las ventas, gastos y stock que trajo — y los
              clientes o proveedores que tocó y nadie editó después. Esto
              deshace{" "}
              <span className="font-semibold text-vk-warning">
                solo la última relectura
              </span>{" "}
              de este archivo, no relecturas anteriores.
            </p>
            <div className="flex items-center justify-end gap-2">
              <button
                type="button"
                onClick={() => setUndoConfirmOpen(false)}
                className="rounded-lg px-3 py-1.5 text-sm font-medium text-vektor-body hover:bg-vektor-surface transition-colors"
              >
                Cancelar
              </button>
              <button
                type="button"
                onClick={() => {
                  rereadUndoMutation.mutate(reread.fileId);
                  setUndoConfirmOpen(false);
                }}
                className="rounded-lg bg-vk-danger px-3 py-1.5 text-sm font-medium text-white hover:brightness-110 transition-colors"
              >
                Sí, deshacer
              </button>
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
}

interface CountCardProps {
  label: string;
  value: number;
  tone: "info" | "success" | "danger" | "muted";
}

const COUNT_TONES: Record<CountCardProps["tone"], string> = {
  info: "text-vk-info",
  success: "text-vk-success",
  danger: "text-vk-danger",
  muted: "text-vk-text-secondary",
};

function CountCard({ label, value, tone }: CountCardProps) {
  return (
    <div className="rounded-lg border border-vk-border-w bg-vk-surface-w px-3 py-2.5 text-center">
      <div className={`text-xl font-semibold ${COUNT_TONES[tone]}`}>{value}</div>
      <div className="mt-0.5 text-xs text-vk-text-muted">{label}</div>
    </div>
  );
}
