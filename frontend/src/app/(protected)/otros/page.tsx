"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { AxiosError } from "axios";
import { Trash2, Pencil, Zap } from "lucide-react";
import { PageWrapper } from "@/components/layout/PageWrapper";
import { Badge } from "@/components/ui/Badge";
import { EmptyState } from "@/components/ui/EmptyState";
import { Modal } from "@/components/ui/Modal";
import { ReclassifyModal } from "./ReclassifyModal";
import { prefill, rowPreview } from "./helpers";
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

  const { data: counts } = useQuery({
    queryKey: ["others-pending-count"],
    queryFn: () => othersService.getPendingCounts(),
    staleTime: 60 * 1000,
  });
  const pendingTotal = counts?.pending ?? 0;
  // Sin una sola sugerencia, "Importar todo lo sugerido" no tiene nada que
  // importar: dispararlo es una importación masiva a ciegas sobre miles de
  // filas cuyo destino Véktor no supo determinar.
  const sinSugerencias = (counts?.pendingSuggested ?? 0) === 0;

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
      const msg =
        `${total} registro(s) importados (${result.imported_sales} ventas, ` +
        `${result.imported_expenses} gastos)` +
        (result.needs_manual > 0
          ? `; ${result.needs_manual} requieren completar la fecha o el monto a mano.`
          : ".");
      toast(msg, "success");
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
      } else if (code === "IDENTITY_CONFLICT") {
        // F5-A: el backend dejó de colapsar el caso ambiguo en un solo id. El
        // código de barras y el SKU pertenecen a productos DISTINTOS: no es un
        // problema de los campos cargados, es una decisión que tiene que tomar el
        // usuario. Sin esta rama caía en el else genérico ("Revisá los campos"),
        // que apunta al lugar equivocado.
        toast(
          "El código de barras y el SKU apuntan a productos distintos. " +
            "Vinculá el registro al que corresponda en vez de crear uno nuevo.",
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
              {sinSugerencias && pendingTotal > 0 ? (
                <span className="block pt-0.5">
                  Ninguno tiene destino sugerido: hay que revisarlos de a uno.
                </span>
              ) : null}
            </p>
            <button
              type="button"
              disabled={bulkImportMutation.isPending || sinSugerencias}
              title={
                sinSugerencias
                  ? "Ninguno de los registros pendientes tiene un destino sugerido: no hay nada que importar automáticamente. Revisalos y asignales destino de a uno."
                  : undefined
              }
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
