"use client";

const ACTION_LABELS: Record<string, string> = {
  REGISTER_SALE: "Registrar venta",
  REGISTER_CASH_INFLOW: "Registrar cobro",
  REGISTER_EXPENSE: "Registrar gasto",
  REGISTER_PURCHASE: "Registrar compra",
  REGISTER_CASH_OUTFLOW: "Registrar pago",
  UPDATE_STOCK: "Actualizar stock",
  UPDATE_PRODUCT: "Actualizar producto",
  REGISTER_STOCK_LOSS: "Registrar merma",
  CREATE_PURCHASE_SUGGESTION: "Sugerir compra",
  IMPORT_TABULAR_FILE: "Importar archivo",
  PARSE_DOCUMENT_FILE: "Procesar documento",
  CREATE_SUPPLIER_DRAFT: "Mensaje al proveedor",
  CREATE_CALENDAR_EVENT: "Evento de calendario",
  SYNC_TO_GOOGLE: "Sincronizar con Google",
};

interface GroupTaskResult {
  action_id: string;
  action_type: string;
  execution_status: string;
}

interface PendingActionGroupStatusProps {
  groupExecutionStatus: "SUCCEEDED" | "PARTIAL_FAILED";
  tasks: GroupTaskResult[];
}

function statusIcon(execution: string): { glyph: string; color: string } {
  if (execution === "SUCCEEDED") return { glyph: "✓", color: "text-emerald-400" };
  if (execution === "REQUIRES_RECONNECT") return { glyph: "↻", color: "text-amber-400" };
  return { glyph: "⚠", color: "text-rose-400" };
}

function statusLabel(execution: string): string {
  if (execution === "SUCCEEDED") return "Completado";
  if (execution === "REQUIRES_RECONNECT") return "Necesita reconectar";
  if (execution === "FAILED") return "No se ejecutó";
  return execution;
}

export function PendingActionGroupStatus({
  groupExecutionStatus,
  tasks,
}: PendingActionGroupStatusProps) {
  const succeededCount = tasks.filter((t) => t.execution_status === "SUCCEEDED").length;
  const total = tasks.length;
  const isFullSuccess = groupExecutionStatus === "SUCCEEDED";

  return (
    <div
      className={`my-2 rounded-lg border p-3 ${
        isFullSuccess
          ? "border-emerald-400/30 bg-emerald-400/5"
          : "border-amber-400/30 bg-amber-400/5"
      }`}
    >
      <div className="mb-2 flex items-center justify-between">
        <p
          className={`text-xs font-medium uppercase tracking-wide ${
            isFullSuccess ? "text-emerald-400" : "text-amber-400"
          }`}
        >
          {isFullSuccess ? "Operaciones completadas" : "Completado parcialmente"}
        </p>
        <span className="text-xs text-vk-text-secondary">
          {succeededCount}/{total}
        </span>
      </div>
      <ul className="space-y-1">
        {tasks.map((t) => {
          const { glyph, color } = statusIcon(t.execution_status);
          const label = ACTION_LABELS[t.action_type] ?? t.action_type;
          return (
            <li
              key={t.action_id}
              className="flex items-center justify-between text-sm text-vk-text-primary"
            >
              <span className="flex items-center gap-2">
                <span className={`${color} font-bold`}>{glyph}</span>
                <span>{label}</span>
              </span>
              <span className="text-xs text-vk-text-secondary">
                {statusLabel(t.execution_status)}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

export type { GroupTaskResult };
