"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { AxiosError } from "axios";
import { Trash2, Pencil, Zap, Link2, Package } from "lucide-react";
import { PageWrapper } from "@/components/layout/PageWrapper";
import { Badge } from "@/components/ui/Badge";
import { EmptyState } from "@/components/ui/EmptyState";
import { Modal } from "@/components/ui/Modal";
import { Table } from "@/components/ui/Table";
import {
  othersService,
  type ProductMatchCandidate,
  type ReclassifyEntityType,
  type UnclassifiedRecordResponse,
} from "@/services/others.service";
import { productsService, type ProductCategoryOption } from "@/services/products.service";
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
  customer: "Cliente",
  supplier: "Proveedor",
};

/** Extrae el `detail.code` de un error de axios (contrato del backend: `{detail:{code}}`). */
function errorCode(error: unknown): string | null {
  const detail = (error as AxiosError<{ detail?: { code?: string } }>)?.response?.data?.detail;
  return detail?.code ?? null;
}

/** Etiqueta corta de un candidato de producto (nombre + sku/barcode si existen). */
function candidateLabel(c: ProductMatchCandidate): string {
  const parts: string[] = [];
  if (c.sku) parts.push(`SKU ${c.sku}`);
  if (c.barcode) parts.push(`EAN ${c.barcode}`);
  return parts.join(" · ");
}

/** Heurística simple de prellenado desde la fila cruda (solo sugerencia visual). */
function prefill(record: UnclassifiedRecordResponse): {
  amount: string;
  date: string;
  text: string;
  quantity: string;
  unitCost: string;
} {
  let amount = "";
  let date = "";
  let text = "";
  let quantity = "1";
  let unitCost = "";
  for (const [key, value] of Object.entries(record.row_data)) {
    const k = key.toLowerCase();
    if (!amount && /(monto|importe|total|precio|valor)/.test(k)) amount = value;
    if (!date && /(fecha|date|dia)/.test(k)) date = value;
    if (!text && /(detalle|concepto|descripcion|nombre|producto|item)/.test(k)) text = value;
    if (quantity === "1" && /^(cantidad|cant|unidades|qty)$/.test(k)) quantity = value;
    if (!unitCost && /(costo|precio.?unit|unitario)/.test(k)) unitCost = value;
  }
  return { amount, date, text, quantity, unitCost };
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

  // Catálogo de categorías de producto del vertical, para el selector del modal.
  const { data: productCategories = [] } = useQuery({
    queryKey: ["product-categories"],
    queryFn: () => productsService.getCategories(),
    staleTime: 30 * 60 * 1000,
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
      targetProductId,
    }: {
      id: string;
      entityType: ReclassifyEntityType;
      fields: Record<string, unknown>;
      targetProductId?: string;
    }) =>
      othersService.reclassify(id, {
        entity_type: entityType,
        fields,
        ...(targetProductId ? { target_product_id: targetProductId } : {}),
      }),
    onSuccess: async (_, vars) => {
      setReclassifying(null);
      toast(
        vars.targetProductId
          ? "Registro vinculado al producto existente."
          : `Registro importado como ${ENTITY_LABELS[vars.entityType].toLowerCase()}.`,
        "success",
      );
      await invalidate();
    },
    onError: async (error) => {
      const code = errorCode(error);
      if (code === "DUPLICATE_PRODUCT_IDENTITY") {
        toast(
          "Ya existe un producto activo con ese SKU o código de barras. " +
            "Vinculalo a uno de los sugeridos en vez de crear uno nuevo.",
          "error",
        );
      } else if (code === "INVALID_TARGET_PRODUCT") {
        // El candidato ya no está disponible (borrado/inactivo): cerrar y refrescar.
        toast("El producto sugerido ya no está disponible. Actualizamos la lista.", "error");
        setReclassifying(null);
        await invalidate();
      } else {
        toast("No se pudo importar el registro. Revisá los campos.", "error");
      }
    },
  });

  const resolvePurchaseMutation = useMutation({
    mutationFn: ({
      id,
      targetProductId,
      fields,
    }: {
      id: string;
      targetProductId: string;
      fields: {
        amount: number;
        quantity: number;
        unitCost?: number;
        transactionDate: string;
        paymentMethod: string;
        category: string;
        description: string;
      };
    }) =>
      othersService.resolvePurchase(id, {
        target_product_id: targetProductId,
        amount: fields.amount,
        quantity: fields.quantity,
        ...(fields.unitCost !== undefined ? { unit_cost: fields.unitCost } : {}),
        transaction_date: fields.transactionDate,
        payment_method: fields.paymentMethod,
        category: fields.category,
        description: fields.description || undefined,
      }),
    onSuccess: async () => {
      setReclassifying(null);
      toast("Compra registrada.", "success");
      await invalidate();
    },
    onError: async (error) => {
      const code = errorCode(error);
      const httpStatus = (error as AxiosError)?.response?.status;
      if (code === "INVALID_TARGET_PRODUCT" || code === "TARGET_NOT_A_CANDIDATE") {
        toast("El producto sugerido ya no está disponible. Actualizamos la lista.", "error");
        setReclassifying(null);
        await invalidate();
      } else if (httpStatus === 409) {
        toast("Esta compra ya había sido registrada. Actualizamos la lista.", "info");
        setReclassifying(null);
        await invalidate();
      } else {
        toast("No se pudo registrar la compra. Revisá los campos.", "error");
      }
    },
  });

  const columns = [
    {
      key: "created_at",
      header: "Fecha",
      render: (_: unknown, row: UnclassifiedRecordResponse) =>
        new Date(row.created_at).toLocaleDateString("es-AR"),
    },
    {
      key: "source",
      header: "Origen",
      render: (_: unknown, row: UnclassifiedRecordResponse) => (
        <Badge variant="info">{SOURCE_LABELS[row.source] ?? row.source}</Badge>
      ),
    },
    {
      key: "_detail",
      header: "Detalle",
      render: (_: unknown, row: UnclassifiedRecordResponse) => {
        const preview = rowPreview(row);
        return (
          <span className="block max-w-md truncate" title={preview}>
            {prefill(row).text || preview}
          </span>
        );
      },
    },
    {
      key: "_amount",
      header: "Monto",
      render: (_: unknown, row: UnclassifiedRecordResponse) => {
        const raw = prefill(row).amount;
        const num = Number(raw.replace(/[^\d.,-]/g, "").replace(",", "."));
        return raw && Number.isFinite(num)
          ? new Intl.NumberFormat("es-AR", {
              style: "currency",
              currency: "ARS",
              maximumFractionDigits: 0,
            }).format(num)
          : "—";
      },
    },
    {
      key: "suggested_entity",
      header: "Destino sugerido",
      render: (_: unknown, row: UnclassifiedRecordResponse) =>
        row.suggested_entity ? (
          <Badge variant="success">{ENTITY_LABELS[row.suggested_entity]}</Badge>
        ) : (
          <span className="text-vk-text-muted">—</span>
        ),
    },
    {
      key: "suggested_category_label",
      header: "Categoría recomendada",
      render: (_: unknown, row: UnclassifiedRecordResponse) =>
        row.suggested_category_label ?? <span className="text-vk-text-muted">—</span>,
    },
    {
      key: "_actions",
      header: "Acciones",
      render: (_: unknown, row: UnclassifiedRecordResponse) => (
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            onClick={() => setReclassifying(row)}
            className="inline-flex items-center gap-1.5 rounded border border-vk-border-w px-2.5 py-1.5 text-sm text-vk-text-primary transition-colors hover:bg-vk-bg-light"
          >
            <Pencil className="h-3.5 w-3.5" /> Editar
          </button>
          <button
            type="button"
            title="Descartar"
            aria-label="Descartar registro"
            disabled={dismissMutation.isPending}
            onClick={() => {
              if (confirm("¿Descartar este registro?")) dismissMutation.mutate(row.id);
            }}
            className="inline-flex h-8 w-8 items-center justify-center rounded border border-vk-danger/30 text-vk-danger transition-colors hover:bg-vk-danger-bg disabled:opacity-50"
          >
            <Trash2 className="h-4 w-4" />
          </button>
        </div>
      ),
    },
  ];

  return (
    <PageWrapper title="Otros">
      <p className="text-sm text-vk-text-muted">
        Datos que llegaron por chat o carga de archivos y no se pudieron clasificar como
        venta, gasto o producto. Revisá la categoría recomendada, editala si hace falta e
        importalos — o descartalos.
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
          <Table columns={columns} data={records} />
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
        productCategories={productCategories}
        saving={reclassifyMutation.isPending || resolvePurchaseMutation.isPending}
        onClose={() => setReclassifying(null)}
        onSave={(entityType, fields) =>
          reclassifying &&
          reclassifyMutation.mutate({ id: reclassifying.id, entityType, fields })
        }
        onLink={(targetProductId) =>
          reclassifying &&
          reclassifyMutation.mutate({
            id: reclassifying.id,
            entityType: "product",
            fields: {},
            targetProductId,
          })
        }
        onResolvePurchase={(targetProductId, fields) =>
          reclassifying &&
          resolvePurchaseMutation.mutate({ id: reclassifying.id, targetProductId, fields })
        }
      />
    </PageWrapper>
  );
}

export function ReclassifyModal({
  record,
  productCategories,
  saving,
  onClose,
  onSave,
  onLink,
  onResolvePurchase,
}: {
  record: UnclassifiedRecordResponse | null;
  productCategories: ProductCategoryOption[];
  saving: boolean;
  onClose: () => void;
  onSave: (entityType: ReclassifyEntityType, fields: Record<string, unknown>) => void;
  onLink: (targetProductId: string) => void;
  onResolvePurchase: (
    targetProductId: string,
    fields: {
      amount: number;
      quantity: number;
      unitCost?: number;
      transactionDate: string;
      paymentMethod: string;
      category: string;
      description: string;
    },
  ) => void;
}) {
  const [entityType, setEntityType] = useState<ReclassifyEntityType>("expense");
  const [amount, setAmount] = useState("");
  const [date, setDate] = useState("");
  const [text, setText] = useState("");
  const [category, setCategory] = useState("OTHER");
  const [productCategory, setProductCategory] = useState("");
  const [paymentMethod, setPaymentMethod] = useState("cash");
  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  const [quantity, setQuantity] = useState("1");
  const [unitCost, setUnitCost] = useState("");

  useEffect(() => {
    if (!record) return;
    const pre = prefill(record);
    const entity = record.suggested_entity ?? "expense";
    setEntityType(entity);
    setAmount(pre.amount.replace(/[^\d.,]/g, ""));
    setDate(pre.date.slice(0, 10));
    setText(pre.text);
    // Prellenar con la categoría recomendada por el backend según el destino.
    setCategory(
      entity === "expense" && record.suggested_category
        ? record.suggested_category
        : entity === "expense" && (record.match_candidates?.length ?? 0) > 0
          ? "INVENTORY"
          : "OTHER",
    );
    setProductCategory(
      entity === "product" && record.suggested_category ? record.suggested_category : "",
    );
    setPaymentMethod("cash");
    setEmail("");
    setPhone("");
    setQuantity(pre.quantity.replace(/[^\d]/g, "") || "1");
    setUnitCost(pre.unitCost.replace(/[^\d.,]/g, ""));
  }, [record]);

  if (!record) return null;

  // F2-T2b: candidatos de producto existente para VINCULAR (solo filas de producto
  // ambiguas/en conflicto). Fuera de ese caso el backend manda null.
  const isPurchaseResolution =
    record.suggested_entity === "expense" && (record.match_candidates?.length ?? 0) > 0;
  const candidates = record.match_candidates ?? [];

  // Cliente y Proveedor son entidades de contacto: solo nombre + email/teléfono.
  const isContact = entityType === "customer" || entityType === "supplier";
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
    } else if (entityType === "product") {
      onSave("product", {
        name: text || "Producto importado",
        sale_price_ars: num,
        category: productCategory || null,
      });
    } else {
      // customer | supplier
      onSave(entityType, {
        name: text,
        ...(email ? { email } : {}),
        ...(phone ? { phone } : {}),
      });
    }
  };

  return (
    <Modal isOpen={!!record} onClose={onClose} title="Editar e importar registro" size="lg">
      <form className="grid gap-4" onSubmit={submit}>
        <div className="rounded border border-vk-border-w bg-vk-bg-light p-3 text-xs text-vk-text-muted">
          {rowPreview(record)}
        </div>

        {candidates.length > 0 ? (
          <div className="grid gap-2 rounded-lg border border-vektor-teal/40 bg-vektor-teal/5 p-3">
            <div className="flex items-center gap-1.5 text-sm font-medium text-vektor-teal">
              <Link2 className="h-4 w-4" />
              Este producto podría ya existir
            </div>
            <p className="text-xs text-vk-text-muted">
              Vinculá el registro a uno de estos productos del catálogo en vez de crear un
              duplicado.
            </p>
            <ul className="grid gap-1.5">
              {candidates.map((c) => {
                const meta = candidateLabel(c);
                return (
                  <li
                    key={c.id}
                    className="flex items-center justify-between gap-3 rounded border border-vk-border-w bg-vk-surface-w px-3 py-2"
                  >
                    <div className="min-w-0">
                      <p className="truncate text-sm text-vk-text-primary">
                        {c.name ?? "Producto sin nombre"}
                      </p>
                      {meta ? (
                        <p className="truncate text-xs text-vk-text-muted">{meta}</p>
                      ) : null}
                    </div>
                    <button
                      type="button"
                      disabled={saving}
                      onClick={() => {
                        if (isPurchaseResolution) {
                          const parsedUnitCost = unitCost
                            ? Number(unitCost.replace(",", "."))
                            : undefined;
                          onResolvePurchase(c.id, {
                            amount: Number(amount.replace(",", ".")),
                            quantity: Number(quantity),
                            ...(parsedUnitCost !== undefined
                              ? { unitCost: parsedUnitCost }
                              : {}),
                            transactionDate: date
                              ? `${date}T00:00:00`
                              : new Date().toISOString().slice(0, 19),
                            paymentMethod,
                            category: category || "INVENTORY",
                            description: text,
                          });
                        } else {
                          onLink(c.id);
                        }
                      }}
                      className="inline-flex shrink-0 items-center gap-1.5 rounded border border-vektor-teal/40 px-2.5 py-1.5 text-xs font-medium text-vektor-teal transition-colors hover:bg-vektor-teal/10 disabled:opacity-50"
                    >
                      <Link2 className="h-3.5 w-3.5" />
                      {isPurchaseResolution ? "Registrar compra" : "Vincular"}
                    </button>
                  </li>
                );
              })}
            </ul>
            <div className="flex items-center gap-1.5 pt-1 text-xs text-vk-text-muted">
              <Package className="h-3.5 w-3.5" />
              {isPurchaseResolution
                ? "Completá monto, fecha y unidades antes de registrar la compra."
                : "…o completá el formulario de abajo para crear un producto nuevo."}
            </div>
          </div>
        ) : null}

        <label className="grid gap-1 text-sm text-vektor-body">
          Importar como
          <select
            className={inputCls}
            value={entityType}
            onChange={(e) => setEntityType(e.target.value as ReclassifyEntityType)}
          >
            <option value="sale">Venta</option>
            <option value="expense">Gasto</option>
            <option value="product">Producto</option>
            <option value="customer">Cliente</option>
            <option value="supplier">Proveedor</option>
          </select>
        </label>
        {!isContact ? (
          <div className="grid grid-cols-2 gap-3">
            <label className="grid gap-1 text-sm text-vektor-body">
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
              <label className="grid gap-1 text-sm text-vektor-body">
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
        ) : null}
        {isPurchaseResolution ? (
          <div className="grid grid-cols-2 gap-3">
            <label className="grid gap-1 text-sm text-vektor-body">
              Cantidad
              <input
                className={inputCls}
                type="number"
                min={1}
                step={1}
                required
                value={quantity}
                onChange={(e) => setQuantity(e.target.value)}
              />
            </label>
            <label className="grid gap-1 text-sm text-vektor-body">
              Costo unitario (opcional)
              <input
                className={inputCls}
                type="number"
                min={0}
                step="0.01"
                value={unitCost}
                onChange={(e) => setUnitCost(e.target.value)}
              />
            </label>
          </div>
        ) : null}
        <label className="grid gap-1 text-sm text-vektor-body">
          {entityType === "product"
            ? "Nombre del producto"
            : entityType === "customer"
              ? "Nombre del cliente"
              : entityType === "supplier"
                ? "Nombre del proveedor"
                : "Descripción"}
          <input
            className={inputCls}
            required={entityType === "product" || isContact}
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
        </label>
        {isContact ? (
          <div className="grid grid-cols-2 gap-3">
            <label className="grid gap-1 text-sm text-vektor-body">
              Email (opcional)
              <input
                className={inputCls}
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </label>
            <label className="grid gap-1 text-sm text-vektor-body">
              Teléfono (opcional)
              <input
                className={inputCls}
                type="tel"
                value={phone}
                onChange={(e) => setPhone(e.target.value)}
              />
            </label>
          </div>
        ) : null}
        {entityType === "expense" ? (
          <label className="grid gap-1 text-sm text-vektor-body">
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
        {entityType === "product" && productCategories.length > 0 ? (
          <label className="grid gap-1 text-sm text-vektor-body">
            Categoría
            <select
              className={inputCls}
              value={productCategory}
              onChange={(e) => setProductCategory(e.target.value)}
            >
              <option value="">Sin categoría</option>
              {productCategories.map((cat) => (
                <option key={cat.code} value={cat.code}>
                  {cat.label}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        {entityType !== "product" && !isContact ? (
          <label className="grid gap-1 text-sm text-vektor-body">
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
