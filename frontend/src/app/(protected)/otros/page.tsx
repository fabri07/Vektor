"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Trash2, FolderInput, Zap } from "lucide-react";
import { PageWrapper } from "@/components/layout/PageWrapper";
import { Badge } from "@/components/ui/Badge";
import { EmptyState } from "@/components/ui/EmptyState";
import { Modal } from "@/components/ui/Modal";
import {
  othersService,
  type ReclassifyEntityType,
  type UnclassifiedRecordResponse,
} from "@/services/others.service";
import { ALL_CATEGORIES, CATEGORY_LABELS } from "@/lib/expenseCategories";
import { useToastStore } from "@/stores/toastStore";

const SOURCE_LABELS: Record<string, string> = {
  ingestion: "Carga de datos",
  chat: "Chat",
  reanalysis: "Reanálisis",
};

const ENTITY_LABELS: Record<ReclassifyEntityType, string> = {
  sale: "Venta",
  expense: "Gasto",
  product: "Producto",
};

/** Heurística simple de prellenado desde la fila cruda (solo sugerencia visual). */
function prefill(record: UnclassifiedRecordResponse): {
  amount: string;
  date: string;
  text: string;
} {
  let amount = "";
  let date = "";
  let text = "";
  for (const [key, value] of Object.entries(record.row_data)) {
    const k = key.toLowerCase();
    if (!amount && /(monto|importe|total|precio|valor)/.test(k)) amount = value;
    if (!date && /(fecha|date|dia)/.test(k)) date = value;
    if (!text && /(detalle|concepto|descripcion|nombre|producto|item)/.test(k)) text = value;
  }
  return { amount, date, text };
}

function rowPreview(record: UnclassifiedRecordResponse): string {
  return Object.entries(record.row_data)
    .filter(([, v]) => v !== "")
    .slice(0, 5)
    .map(([k, v]) => `${k}: ${v}`)
    .join(" · ");
}

const PAGE_SIZE = 50;

export default function OtrosPage() {
  const [reclassifying, setReclassifying] = useState<UnclassifiedRecordResponse | null>(null);
  const [page, setPage] = useState(0);
  const queryClient = useQueryClient();
  const toast = useToastStore((s) => s.add);

  const { data: records = [], isLoading, isError } = useQuery({
    queryKey: ["others-pending", page],
    queryFn: () => othersService.getPending(page * PAGE_SIZE, PAGE_SIZE),
    staleTime: 60 * 1000,
  });

  const { data: pendingTotal = 0 } = useQuery({
    queryKey: ["others-pending-count"],
    queryFn: () => othersService.getPendingCount(),
    staleTime: 60 * 1000,
  });

  const totalPages = Math.max(1, Math.ceil(pendingTotal / PAGE_SIZE));

  const invalidate = async () => {
    await queryClient.invalidateQueries({ queryKey: ["others-pending"] });
    await queryClient.invalidateQueries({ queryKey: ["others-pending-count"] });
  };

  const dismissMutation = useMutation({
    mutationFn: (id: string) => othersService.dismiss(id),
    onSuccess: async () => {
      toast("Registro descartado.", "success");
      await invalidate();
    },
    onError: () => toast("No se pudo descartar el registro.", "error"),
  });

  const bulkImportMutation = useMutation({
    mutationFn: () => othersService.bulkImport(),
    onSuccess: async (result) => {
      const total = result.imported_sales + result.imported_expenses;
      toast(
        `${total} registro(s) importados (${result.imported_sales} ventas, ` +
          `${result.imported_expenses} gastos)` +
          (result.skipped > 0
            ? `; ${result.skipped} quedaron pendientes (sin fecha o monto legibles).`
            : "."),
        "success",
      );
      await invalidate();
    },
    onError: () => toast("No se pudo importar en lote. Probá de nuevo.", "error"),
  });

  const reclassifyMutation = useMutation({
    mutationFn: ({
      id,
      entityType,
      fields,
    }: {
      id: string;
      entityType: ReclassifyEntityType;
      fields: Record<string, unknown>;
    }) => othersService.reclassify(id, { entity_type: entityType, fields }),
    onSuccess: async (_, vars) => {
      setReclassifying(null);
      toast(`Registro importado como ${ENTITY_LABELS[vars.entityType].toLowerCase()}.`, "success");
      await invalidate();
    },
    onError: () => toast("No se pudo importar el registro. Revisá los campos.", "error"),
  });

  return (
    <PageWrapper title="Otros">
      <p className="text-sm text-vk-text-muted">
        Datos que llegaron por chat o carga de archivos y no se pudieron clasificar como
        venta, gasto o producto. Importalos asignándoles un tipo, o descartalos.
      </p>

      {isError ? (
        <p className="rounded-lg border border-vk-danger/20 bg-vk-danger-bg px-4 py-3 text-sm text-vk-danger">
          Error al cargar los registros. Recargá la página.
        </p>
      ) : isLoading ? (
        <div className="h-40 animate-pulse rounded-lg border border-vk-border-w bg-vk-surface-w" />
      ) : records.length === 0 ? (
        <EmptyState
          title="Nada pendiente de revisión"
          description="Todo lo que Véktor no pueda clasificar automáticamente va a aparecer acá."
        />
      ) : (
        <>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-xs text-vk-text-muted">
            {pendingTotal} registro(s) pendiente(s)
            {totalPages > 1 ? ` — página ${page + 1} de ${totalPages}` : ""}
          </p>
          <button
            type="button"
            disabled={bulkImportMutation.isPending}
            onClick={() => {
              if (
                confirm(
                  "¿Importar TODOS los registros pendientes sugeridos como venta o gasto? " +
                    "Cada uno se registra en su sección con la fecha, monto y categoría detectados. " +
                    "Los que no tengan fecha o monto legibles quedan pendientes para revisión manual.",
                )
              )
                bulkImportMutation.mutate();
            }}
            className="inline-flex items-center gap-1.5 rounded border border-vektor-teal/40 px-3 py-1.5 text-sm text-vektor-teal transition-colors hover:bg-vektor-teal/10 disabled:opacity-50"
          >
            <Zap className="h-4 w-4" />
            {bulkImportMutation.isPending ? "Importando…" : "Importar todo lo sugerido"}
          </button>
        </div>
        <ul className="space-y-3">
          {records.map((record) => (
            <li
              key={record.id}
              className="flex flex-col gap-2 rounded-lg border border-vk-border-w bg-vk-surface-w p-4 sm:flex-row sm:items-center sm:justify-between"
            >
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant="info">{SOURCE_LABELS[record.source] ?? record.source}</Badge>
                  {record.suggested_entity ? (
                    <Badge variant="success">
                      Sugerido: {ENTITY_LABELS[record.suggested_entity]}
                    </Badge>
                  ) : null}
                  {record.context_label ? (
                    <span className="text-xs text-vk-text-muted">{record.context_label}</span>
                  ) : null}
                  <span className="text-xs text-vk-text-muted">
                    {new Date(record.created_at).toLocaleDateString("es-AR")}
                  </span>
                </div>
                <p className="mt-1 truncate text-sm text-vk-text-primary">{rowPreview(record)}</p>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <button
                  type="button"
                  onClick={() => setReclassifying(record)}
                  className="inline-flex items-center gap-1.5 rounded border border-vk-border-w px-3 py-1.5 text-sm text-vk-text-primary transition-colors hover:bg-vk-bg-light"
                >
                  <FolderInput className="h-4 w-4" /> Importar como…
                </button>
                <button
                  type="button"
                  title="Descartar"
                  aria-label="Descartar registro"
                  disabled={dismissMutation.isPending}
                  onClick={() => {
                    if (confirm("¿Descartar este registro?")) dismissMutation.mutate(record.id);
                  }}
                  className="inline-flex h-8 w-8 items-center justify-center rounded border border-vk-danger/30 text-vk-danger transition-colors hover:bg-vk-danger-bg disabled:opacity-50"
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>
            </li>
          ))}
        </ul>
        {totalPages > 1 ? (
          <div className="flex items-center justify-center gap-3 pt-2">
            <button
              type="button"
              disabled={page === 0}
              onClick={() => setPage((p) => Math.max(0, p - 1))}
              className="rounded border border-vk-border-w px-3 py-1.5 text-sm text-vk-text-primary disabled:opacity-40"
            >
              ← Anterior
            </button>
            <button
              type="button"
              disabled={page + 1 >= totalPages}
              onClick={() => setPage((p) => p + 1)}
              className="rounded border border-vk-border-w px-3 py-1.5 text-sm text-vk-text-primary disabled:opacity-40"
            >
              Siguiente →
            </button>
          </div>
        ) : null}
        </>
      )}

      <ReclassifyModal
        record={reclassifying}
        saving={reclassifyMutation.isPending}
        onClose={() => setReclassifying(null)}
        onSave={(entityType, fields) =>
          reclassifying &&
          reclassifyMutation.mutate({ id: reclassifying.id, entityType, fields })
        }
      />
    </PageWrapper>
  );
}

function ReclassifyModal({
  record,
  saving,
  onClose,
  onSave,
}: {
  record: UnclassifiedRecordResponse | null;
  saving: boolean;
  onClose: () => void;
  onSave: (entityType: ReclassifyEntityType, fields: Record<string, unknown>) => void;
}) {
  const [entityType, setEntityType] = useState<ReclassifyEntityType>("expense");
  const [amount, setAmount] = useState("");
  const [date, setDate] = useState("");
  const [text, setText] = useState("");
  const [category, setCategory] = useState("OTHER");
  const [paymentMethod, setPaymentMethod] = useState("cash");

  useEffect(() => {
    if (!record) return;
    const pre = prefill(record);
    setEntityType(record.suggested_entity ?? "expense");
    setAmount(pre.amount.replace(/[^\d.,]/g, ""));
    setDate(pre.date.slice(0, 10));
    setText(pre.text);
    setCategory("OTHER");
    setPaymentMethod("cash");
  }, [record]);

  if (!record) return null;

  const inputCls = "rounded border border-vk-border-w px-3 py-2";
  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    const num = Number(amount.replace(",", "."));
    const isoDate = date ? `${date}T00:00:00` : new Date().toISOString().slice(0, 19);
    if (entityType === "sale") {
      onSave("sale", {
        amount: num,
        transaction_date: isoDate,
        payment_method: paymentMethod,
        notes: text || null,
      });
    } else if (entityType === "expense") {
      onSave("expense", {
        amount: num,
        expense_date: isoDate,
        category,
        description: text,
        payment_method: paymentMethod,
      });
    } else {
      onSave("product", {
        name: text || "Producto importado",
        sale_price_ars: num,
      });
    }
  };

  return (
    <Modal isOpen={!!record} onClose={onClose} title="Importar registro" size="lg">
      <form className="grid gap-4" onSubmit={submit}>
        <div className="rounded border border-vk-border-w bg-vk-bg-light p-3 text-xs text-vk-text-muted">
          {rowPreview(record)}
        </div>
        <label className="grid gap-1 text-sm text-vk-text-secondary">
          Importar como
          <select
            className={inputCls}
            value={entityType}
            onChange={(e) => setEntityType(e.target.value as ReclassifyEntityType)}
          >
            <option value="sale">Venta</option>
            <option value="expense">Gasto</option>
            <option value="product">Producto</option>
          </select>
        </label>
        <div className="grid grid-cols-2 gap-3">
          <label className="grid gap-1 text-sm text-vk-text-secondary">
            {entityType === "product" ? "Precio de venta" : "Monto"}
            <input
              className={inputCls}
              type="number"
              min={0}
              step="0.01"
              required
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
            />
          </label>
          {entityType !== "product" ? (
            <label className="grid gap-1 text-sm text-vk-text-secondary">
              Fecha
              <input
                className={inputCls}
                type="date"
                required
                value={date}
                onChange={(e) => setDate(e.target.value)}
              />
            </label>
          ) : null}
        </div>
        <label className="grid gap-1 text-sm text-vk-text-secondary">
          {entityType === "product" ? "Nombre del producto" : "Descripción"}
          <input
            className={inputCls}
            required={entityType === "product"}
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
        </label>
        {entityType === "expense" ? (
          <label className="grid gap-1 text-sm text-vk-text-secondary">
            Categoría
            <select
              className={inputCls}
              value={category}
              onChange={(e) => setCategory(e.target.value)}
            >
              {ALL_CATEGORIES.map((cat) => (
                <option key={cat} value={cat}>
                  {CATEGORY_LABELS[cat]}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        {entityType !== "product" ? (
          <label className="grid gap-1 text-sm text-vk-text-secondary">
            Forma de pago
            <select
              className={inputCls}
              value={paymentMethod}
              onChange={(e) => setPaymentMethod(e.target.value)}
            >
              <option value="cash">Efectivo</option>
              <option value="transfer">Transferencia</option>
              <option value="debit_card">Débito</option>
              <option value="credit_card">Crédito</option>
              <option value="qr">QR</option>
              <option value="account">Cuenta corriente</option>
            </select>
          </label>
        ) : null}
        <div className="flex justify-end gap-2 pt-2">
          <button type="button" onClick={onClose} className="rounded border border-vk-border-w px-4 py-2 text-sm">
            Cancelar
          </button>
          <button
            type="submit"
            disabled={saving}
            className="rounded bg-vk-blue px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
          >
            {saving ? "Importando..." : "Importar"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
