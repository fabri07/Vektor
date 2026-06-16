"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil, Trash2, Eye } from "lucide-react";
import { PageWrapper } from "@/components/layout/PageWrapper";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Badge } from "@/components/ui/Badge";
import { Modal } from "@/components/ui/Modal";
import { EmptyState } from "@/components/ui/EmptyState";
import { SmartTable } from "@/components/ui/SmartTable";
import {
  suppliersService,
  type CreateSupplierPayload,
  type SupplierResponse,
} from "@/services/suppliers.service";
import { expensesService, type ExpenseEntryResponse } from "@/services/expenses.service";
import { categoryDisplay } from "@/lib/expenseCategories";
import { formatDateTime } from "@/lib/datetime";
import { useToastStore } from "@/stores/toastStore";

const PAYMENT_METHOD_LABELS: Record<string, string> = {
  cash: "Efectivo",
  debit_card: "Tarjeta débito",
  credit_card: "Tarjeta crédito",
  transfer: "Transferencia",
  qr: "QR / Mercado Pago",
  account: "Cuenta corriente",
  other: "Otro",
};

function paymentLabel(method: string): string {
  return PAYMENT_METHOD_LABELS[method] ?? method;
}

function formatARS(value: number): string {
  return new Intl.NumberFormat("es-AR", {
    style: "currency",
    currency: "ARS",
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  }).format(value);
}

const COLUMNS = [
  {
    key: "name",
    header: "Nombre",
    hideable: true,
    render: (v: unknown) => (
      <span className="font-medium text-vk-text-primary">{String(v)}</span>
    ),
    csvValue: (v: unknown) => String(v ?? ""),
  },
  {
    key: "email",
    header: "Email",
    hideable: true,
    render: (v: unknown) => String(v ?? "").trim() || "—",
    csvValue: (v: unknown) => String(v ?? "").trim(),
  },
  {
    key: "phone",
    header: "Teléfono",
    hideable: true,
    render: (v: unknown) => String(v ?? "").trim() || "—",
    csvValue: (v: unknown) => String(v ?? "").trim(),
  },
  {
    key: "_status",
    header: "Estado",
    hideable: true,
    render: (_: unknown, row: Record<string, unknown>) =>
      (row as unknown as SupplierResponse).is_active ? (
        <Badge variant="success">Activo</Badge>
      ) : (
        <Badge variant="default">Inactivo</Badge>
      ),
    csvValue: (_: unknown, row: Record<string, unknown>) =>
      (row as unknown as SupplierResponse).is_active ? "Activo" : "Inactivo",
  },
  {
    key: "created_at",
    header: "Creado",
    hideable: true,
    defaultVisible: false,
    render: (v: unknown) => formatDateTime(v),
    csvValue: (v: unknown) => formatDateTime(v),
  },
];

export default function SuppliersPage() {
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<SupplierResponse | null>(null);
  const [viewing, setViewing] = useState<SupplierResponse | null>(null);
  const queryClient = useQueryClient();
  const toast = useToastStore((s) => s.add);

  const { data: suppliers = [], isLoading, isError } = useQuery({
    queryKey: ["suppliers-list"],
    queryFn: () => suppliersService.getAllSuppliers(),
    staleTime: 2 * 60 * 1000,
  });

  const createMutation = useMutation({
    mutationFn: (payload: CreateSupplierPayload) =>
      suppliersService.createSupplier(payload),
    onSuccess: async () => {
      setCreating(false);
      toast("Proveedor creado.", "success");
      await queryClient.invalidateQueries({ queryKey: ["suppliers-list"] });
    },
    onError: () => toast("No se pudo crear el proveedor.", "error"),
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: CreateSupplierPayload }) =>
      suppliersService.updateSupplier(id, payload),
    onSuccess: async () => {
      setEditing(null);
      toast("Proveedor actualizado.", "success");
      await queryClient.invalidateQueries({ queryKey: ["suppliers-list"] });
    },
    onError: () => toast("No se pudo actualizar el proveedor.", "error"),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => suppliersService.deleteSupplier(id),
    onSuccess: async () => {
      toast("Proveedor eliminado.", "success");
      await queryClient.invalidateQueries({ queryKey: ["suppliers-list"] });
    },
    onError: () => toast("No se pudo eliminar el proveedor.", "error"),
  });

  const tableData = suppliers.map((s) => ({ ...s, _status: null }));

  return (
    <PageWrapper
      title="Proveedores"
      actions={
        <Button size="sm" onClick={() => setCreating(true)}>
          Nuevo proveedor
        </Button>
      }
    >
      {isLoading ? (
        <div className="space-y-2">
          {[...Array<number>(4)].map((_, i) => (
            <div
              key={i}
              className="h-12 animate-pulse rounded-lg border border-vk-border-w bg-vk-surface-w"
            />
          ))}
        </div>
      ) : isError ? (
        <p className="rounded-lg border border-vk-danger/20 bg-vk-danger-bg px-4 py-3 text-sm text-vk-danger">
          Error al cargar los proveedores. Recargá la página.
        </p>
      ) : suppliers.length === 0 ? (
        <EmptyState
          title="Sin proveedores cargados"
          description="Agregá tu primer proveedor con el botón 'Nuevo proveedor'."
        />
      ) : (
        <SmartTable
          columns={COLUMNS}
          data={tableData as Record<string, unknown>[]}
          exportFilename="vektor-proveedores"
          renderActions={(row) => {
            const supplier = row as unknown as SupplierResponse;
            return (
              <div className="flex items-center gap-1">
                <button
                  type="button"
                  title="Ver"
                  aria-label="Ver proveedor"
                  onClick={() => setViewing(supplier)}
                  className="inline-flex h-8 w-8 items-center justify-center rounded border border-vk-border-w text-vk-text-secondary transition-colors hover:bg-vk-bg-light hover:text-vk-text-primary"
                >
                  <Eye className="h-4 w-4" />
                </button>
                <button
                  type="button"
                  title="Editar"
                  aria-label="Editar proveedor"
                  onClick={() => setEditing(supplier)}
                  className="inline-flex h-8 w-8 items-center justify-center rounded border border-vk-border-w text-vk-text-secondary transition-colors hover:bg-vk-bg-light hover:text-vk-text-primary"
                >
                  <Pencil className="h-4 w-4" />
                </button>
                <button
                  type="button"
                  title="Eliminar"
                  aria-label="Eliminar proveedor"
                  disabled={deleteMutation.isPending}
                  onClick={() => {
                    if (confirm("¿Eliminar este proveedor?")) deleteMutation.mutate(supplier.id);
                  }}
                  className="inline-flex h-8 w-8 items-center justify-center rounded border border-vk-danger/30 text-vk-danger transition-colors hover:bg-vk-danger-bg disabled:opacity-50"
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>
            );
          }}
        />
      )}

      <SupplierFormModal
        title="Nuevo proveedor"
        supplier={null}
        isOpen={creating}
        saving={createMutation.isPending}
        onClose={() => setCreating(false)}
        onSave={(payload) => createMutation.mutate(payload)}
      />

      <SupplierFormModal
        title="Editar proveedor"
        supplier={editing}
        isOpen={!!editing}
        saving={updateMutation.isPending}
        onClose={() => setEditing(null)}
        onSave={(payload) => {
          if (editing) updateMutation.mutate({ id: editing.id, payload });
        }}
      />

      <SupplierDetailModal supplier={viewing} onClose={() => setViewing(null)} />
    </PageWrapper>
  );
}

// ── Form modal (create + edit) ─────────────────────────────────────────────────

interface SupplierFormState {
  name: string;
  email: string;
  phone: string;
  notes: string;
}

function toFormState(supplier: SupplierResponse | null): SupplierFormState {
  return {
    name: supplier?.name ?? "",
    email: supplier?.email ?? "",
    phone: supplier?.phone ?? "",
    notes: supplier?.notes ?? "",
  };
}

function SupplierFormModal({
  title,
  supplier,
  isOpen,
  saving,
  onClose,
  onSave,
}: {
  title: string;
  supplier: SupplierResponse | null;
  isOpen: boolean;
  saving: boolean;
  onClose: () => void;
  onSave: (payload: CreateSupplierPayload) => void;
}) {
  const [form, setForm] = useState<SupplierFormState>(() => toFormState(supplier));
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (isOpen) {
      setForm(toFormState(supplier));
      setError(null);
    }
  }, [isOpen, supplier]);

  function set(key: keyof SupplierFormState) {
    return (e: React.ChangeEvent<HTMLInputElement>) =>
      setForm((prev) => ({ ...prev, [key]: e.target.value }));
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!form.name.trim()) {
      setError("El nombre es requerido.");
      return;
    }
    onSave({
      name: form.name.trim(),
      email: form.email.trim() || null,
      phone: form.phone.trim() || null,
      notes: form.notes.trim() || null,
    });
  }

  return (
    <Modal isOpen={isOpen} onClose={onClose} title={title} size="lg">
      <form className="space-y-4" onSubmit={handleSubmit}>
        <Input
          label="Nombre"
          type="text"
          placeholder="Ej: Distribuidora del Sur"
          value={form.name}
          onChange={set("name")}
          error={error ?? undefined}
        />
        <div className="grid grid-cols-2 gap-4">
          <Input
            label="Email (opcional)"
            type="email"
            placeholder="proveedor@email.com"
            value={form.email}
            onChange={set("email")}
          />
          <Input
            label="Teléfono (opcional)"
            type="text"
            placeholder="+54 9 11 1234-5678"
            value={form.phone}
            onChange={set("phone")}
          />
        </div>
        <Input
          label="Notas (opcional)"
          type="text"
          placeholder="Ej: entrega los martes, paga a 30 días..."
          value={form.notes}
          onChange={set("notes")}
        />
        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" variant="secondary" size="sm" onClick={onClose}>
            Cancelar
          </Button>
          <Button type="submit" size="sm" loading={saving}>
            Guardar
          </Button>
        </div>
      </form>
    </Modal>
  );
}

// ── Detail modal (ficha + compras asociadas) ──────────────────────────────────

function SupplierDetailModal({
  supplier,
  onClose,
}: {
  supplier: SupplierResponse | null;
  onClose: () => void;
}) {
  const { data: expenses = [], isLoading } = useQuery({
    queryKey: ["expenses-by-supplier", supplier?.id],
    queryFn: () => expensesService.getAllEntries({ supplier_id: supplier!.id }),
    enabled: !!supplier,
    staleTime: 60 * 1000,
  });

  if (!supplier) return null;

  const totalExpenses = expenses.reduce(
    (s: number, exp: ExpenseEntryResponse) => s + exp.amount,
    0,
  );

  return (
    <Modal isOpen={!!supplier} onClose={onClose} title={supplier.name} size="2xl">
      <div className="space-y-5">
        {/* Datos de contacto */}
        <section className="grid grid-cols-2 gap-3 text-sm">
          <div>
            <p className="text-xs text-vk-text-muted">Email</p>
            <p className="text-vk-text-primary">{supplier.email?.trim() || "—"}</p>
          </div>
          <div>
            <p className="text-xs text-vk-text-muted">Teléfono</p>
            <p className="text-vk-text-primary">{supplier.phone?.trim() || "—"}</p>
          </div>
          <div>
            <p className="text-xs text-vk-text-muted">Estado</p>
            <p>
              {supplier.is_active ? (
                <Badge variant="success">Activo</Badge>
              ) : (
                <Badge variant="default">Inactivo</Badge>
              )}
            </p>
          </div>
          {supplier.notes?.trim() ? (
            <div className="col-span-2">
              <p className="text-xs text-vk-text-muted">Notas</p>
              <p className="text-vk-text-primary">{supplier.notes}</p>
            </div>
          ) : null}
        </section>

        {/* Compras asociadas */}
        <section className="space-y-2">
          <div className="flex items-center justify-between">
            <h3 className="font-display text-sm font-semibold text-vk-text-primary">
              Compras asociadas
            </h3>
            {expenses.length > 0 && (
              <span className="text-xs text-vk-text-muted">
                {expenses.length} compra(s) · {formatARS(totalExpenses)}
              </span>
            )}
          </div>

          {isLoading ? (
            <div className="space-y-2">
              {[...Array<number>(3)].map((_, i) => (
                <div
                  key={i}
                  className="h-9 animate-pulse rounded-lg border border-vk-border-w bg-vk-surface-w"
                />
              ))}
            </div>
          ) : expenses.length === 0 ? (
            <p className="rounded-lg border border-vk-border-w bg-vk-surface-w px-4 py-3 text-sm text-vk-text-muted">
              Este proveedor no tiene compras asociadas todavía.
            </p>
          ) : (
            <div className="overflow-hidden rounded-lg border border-vk-border-w">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-vk-border-w bg-vk-bg-light text-left">
                    <th className="px-3 py-2 text-xs font-semibold text-vk-text-secondary">Fecha</th>
                    <th className="px-3 py-2 text-xs font-semibold text-vk-text-secondary">Monto</th>
                    <th className="px-3 py-2 text-xs font-semibold text-vk-text-secondary">Categoría</th>
                    <th className="px-3 py-2 text-xs font-semibold text-vk-text-secondary">Método de pago</th>
                  </tr>
                </thead>
                <tbody>
                  {expenses.map((exp: ExpenseEntryResponse) => (
                    <tr key={exp.id} className="border-b border-vk-border-w last:border-b-0">
                      <td className="px-3 py-2 text-vk-text-primary">
                        {formatDateTime(exp.transaction_date)}
                      </td>
                      <td className="px-3 py-2 text-vk-text-primary">{formatARS(exp.amount)}</td>
                      <td className="px-3 py-2 text-vk-text-secondary">
                        {categoryDisplay(exp.category, exp.category_label)}
                      </td>
                      <td className="px-3 py-2 text-vk-text-secondary">
                        {paymentLabel(exp.payment_method)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>
    </Modal>
  );
}
