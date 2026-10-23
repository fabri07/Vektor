"use client";

import { useQuery } from "@tanstack/react-query";

import { Modal } from "@/components/ui/Modal";
import {
  ingestionService,
  type ImportReceiptExecution,
} from "@/services/ingestion.service";

const RESULT_LABELS: Record<string, string> = {
  guardado: "Guardado",
  transformado: "Transformado",
  pendiente: "Pendiente en «Otros»",
  excluido: "Excluido",
  mixto: "Parcial",
};

const RESULT_COLORS: Record<string, string> = {
  guardado: "text-vk-success bg-vk-success-bg",
  transformado: "text-vk-info bg-vk-info-bg",
  pendiente: "text-vk-warning bg-vk-warning-bg",
  excluido: "text-vk-text-muted bg-vk-border-w",
  mixto: "text-vk-warning bg-vk-warning-bg",
};

const REASON_KIND_LABELS: Record<string, string> = {
  user_decision: "decisión tuya",
  system_rule: "regla del sistema",
};

const ATTEMPT_STATUS_LABELS: Record<string, string> = {
  FAILED: "Falló",
  REVERTED: "Revertida",
  APPLYING: "Aplicándose",
};

function formatDate(iso: string): string {
  return new Date(iso).toLocaleString("es-AR", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** Cambio 4 — tabla columna por columna de un comprobante. Compartida entre
 * "última aplicación" y "último intento": es el mismo contrato. */
function ReceiptTable({ execution }: { execution: ImportReceiptExecution }) {
  const { columns, historical_incomplete } = execution.receipt;
  return (
    <div className="flex flex-col gap-2">
      {historical_incomplete && (
        <p className="rounded-lg border border-vk-border-w bg-vk-bg-light px-3 py-2 text-xs text-vk-text-muted">
          Import anterior al comprobante detallado: se reconstruyó lo
          demostrable desde el mapeo guardado. El resto no quedó registrado.
        </p>
      )}
      {columns.length === 0 ? (
        <p className="text-xs text-vk-text-muted">
          No hay detalle por columna para esta ejecución.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-vk-border-w text-left text-vk-text-muted">
                <th className="py-1.5 pr-3 font-medium">Hoja</th>
                <th className="py-1.5 pr-3 font-medium">Columna</th>
                <th className="py-1.5 pr-3 font-medium">Destino</th>
                <th className="py-1.5 pr-3 font-medium">Resultado</th>
                <th className="py-1.5 font-medium">Motivo</th>
              </tr>
            </thead>
            <tbody>
              {columns.map((c) => (
                <tr
                  key={`${c.context_id}:${c.source_column}`}
                  className="border-b border-vk-border-w/60"
                >
                  <td className="py-1.5 pr-3 text-vk-text-secondary">
                    {c.context_label}
                  </td>
                  <td className="py-1.5 pr-3 font-mono text-vk-text-primary">
                    {c.source_column}
                  </td>
                  <td className="py-1.5 pr-3 text-vk-text-secondary">
                    {c.target_field ?? "—"}
                  </td>
                  <td className="py-1.5 pr-3">
                    <span
                      className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${
                        RESULT_COLORS[c.result] ?? "text-vk-text-muted bg-vk-border-w"
                      }`}
                    >
                      {RESULT_LABELS[c.result] ?? c.result}
                    </span>
                  </td>
                  <td className="py-1.5 text-vk-text-secondary">
                    {c.reason ?? "—"}
                    {c.reason_kind && (
                      <span className="ml-1 text-vk-text-muted">
                        ({REASON_KIND_LABELS[c.reason_kind] ?? c.reason_kind})
                      </span>
                    )}
                    {c.rows_affected !== null && (
                      <span className="ml-1 text-vk-text-muted">
                        · {c.rows_affected} fila(s)
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

interface ImportReceiptModalProps {
  fileId: string | null;
  filename: string;
  onClose: () => void;
}

/**
 * Cambio 4 — comprobante de importación de un archivo: qué pasó con cada
 * columna en la última ejecución, separando la aplicación vigente del
 * intento más reciente (puede haber fallado o haberse revertido después).
 */
export function ImportReceiptModal({ fileId, filename, onClose }: ImportReceiptModalProps) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["import-receipt", fileId],
    queryFn: () => ingestionService.getFileReceipt(fileId as string),
    enabled: !!fileId,
  });

  return (
    <Modal isOpen={fileId !== null} onClose={onClose} title="Comprobante de importación" size="2xl">
      <div className="flex flex-col gap-4">
        <p className="text-sm text-vk-text-secondary">
          <span className="font-medium text-vk-text-primary">{filename}</span>
        </p>

        {isLoading && (
          <div className="flex items-center gap-2 text-sm text-vk-text-muted">
            <div className="h-4 w-4 animate-spin rounded-full border-2 border-vk-border-w border-t-vk-blue" />
            Cargando comprobante…
          </div>
        )}

        {isError && (
          <p className="text-sm text-vk-danger">
            No se pudo cargar el comprobante de este archivo.
          </p>
        )}

        {data && (
          <>
            {data.file_reverted && (
              <div className="rounded-lg border border-vk-warning/40 bg-vk-warning-bg px-3 py-2 text-xs text-vk-warning">
                Este archivo fue eliminado. Lo que aparece abajo es lo que se
                guardó en su momento — la explicación no se borra junto con el
                archivo.
              </div>
            )}

            {data.last_applied ? (
              <div className="flex flex-col gap-2">
                <h3 className="text-xs font-semibold uppercase tracking-wide text-vk-text-muted">
                  Última aplicación vigente ·{" "}
                  {data.last_applied.kind === "confirm" ? "Confirmación" : "Relectura"}{" "}
                  · {formatDate(data.last_applied.applied_at)}
                </h3>
                <ReceiptTable execution={data.last_applied} />
              </div>
            ) : (
              <p className="text-sm text-vk-text-muted">
                No hay ninguna ejecución aplicada vigente para este archivo.
              </p>
            )}

            {data.last_attempt && (
              <div className="flex flex-col gap-2 border-t border-vk-border-w pt-3">
                <h3 className="text-xs font-semibold uppercase tracking-wide text-vk-warning">
                  Último intento de relectura ·{" "}
                  {ATTEMPT_STATUS_LABELS[data.last_attempt.status] ?? data.last_attempt.status}{" "}
                  · {formatDate(data.last_attempt.at)}
                </h3>
                {data.last_attempt.error && (
                  <p className="text-xs text-vk-text-secondary">{data.last_attempt.error}</p>
                )}
                <ReceiptTable execution={data.last_attempt} />
              </div>
            )}
          </>
        )}
      </div>
    </Modal>
  );
}
