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
  customersService,
  type CreateCustomerPayload,
  type CustomerResponse,
} from "@/services/customers.service";
import { salesService, type SaleEntryResponse } from "@/services/sales.service";
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
    key: "telegram_username",
    header: "Telegram",
    hideable: true,
    defaultVisible: false,
    render: (v: unknown) => String(v ?? "").trim() || "—",
    csvValue: (v: unknown) => String(v ?? "").trim(),
  },
  {
    key: "_status",
    header: "Estado",
    hideable: true,
    render: (_: unknown, row: Record<string, unknown>) =>
      (row as unknown as CustomerResponse).is_active ? (
        <Badge variant="success">Activo</Badge>
      ) : (
        <Badge variant="default">Inactivo</Badge>
      ),
    csvValue: (_: unknown, row: Record<string, unknown>) =>
      (row as unknown as CustomerResponse).is_active ? "Activo" : "Inactivo",
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

export default function CustomersPage() {
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<CustomerResponse | null>(null);
  const [viewing, setViewing] = useState<CustomerResponse | null>(null);
  const queryClient = useQueryClient();
  const toast = useToastStore((s) => s.add);

  const { data: customers = [], isLoading, isError } = useQuery({
    queryKey: ["customers-list"],
    queryFn: () => customersService.getAllCustomers(),
    staleTime: 2 * 60 * 1000,
  });

  const createMutation = useMutation({
    mutationFn: (payload: CreateCustomerPayload) =>
      customersService.createCustomer(payload),
    onSuccess: async () => {
      setCreating(false);
      toast("Cliente creado.", "success");
      await queryClient.invalidateQueries({ queryKey: ["customers-list"] });
    },
    onError: () => toast("No se pudo crear el cliente.", "error"),
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: CreateCustomerPayload }) =>
      customersService.updateCustomer(id, payload),
    onSuccess: async () => {
      setEditing(null);
      toast("Cliente actualizado.", "success");
      await queryClient.invalidateQueries({ queryKey: ["customers-list"] });
    },
    onError: () => toast("No se pudo actualizar el cliente.", "error"),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => customersService.deleteCustomer(id),
    onSuccess: async () => {
      toast("Cliente eliminado.", "success");
      await queryClient.invalidateQueries({ queryKey: ["customers-list"] });
    },
    onError: () => toast("No se pudo eliminar el cliente.", "error"),
  });

  const tableData = customers.map((c) => ({ ...c, _status: null }));

  return (
    <PageWrapper
      title="Clientes"
      actions={
        <Button size="sm" onClick={() => setCreating(true)}>
          Nuevo cliente
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
          Error al cargar los clientes. Recargá la página.
        </p>
      ) : customers.length === 0 ? (
        <EmptyState
          title="Sin clientes cargados"
          description="Agregá tu primer cliente con el botón 'Nuevo cliente'."
        />
      ) : (
        <SmartTable
          columns={COLUMNS}
          data={tableData as Record<string, unknown>[]}
          exportFilename="vektor-clientes"
          renderActions={(row) => {
            const customer = row as unknown as CustomerResponse;
            return (
              <div className="flex items-center gap-1">
                <button
                  type="button"
                  title="Ver"
                  aria-label="Ver cliente"
                  onClick={() => setViewing(customer)}
                  className="inline-flex h-8 w-8 items-center justify-center rounded border border-vk-border-w text-vk-text-secondary transition-colors hover:bg-vk-bg-light hover:text-vk-text-primary"
                >
                  <Eye className="h-4 w-4" />
                </button>
                <button
                  type="button"
                  title="Editar"
                  aria-label="Editar cliente"
                  onClick={() => setEditing(customer)}
                  className="inline-flex h-8 w-8 items-center justify-center rounded border border-vk-border-w text-vk-text-secondary transition-colors hover:bg-vk-bg-light hover:text-vk-text-primary"
                >
                  <Pencil className="h-4 w-4" />
                </button>
                <button
                  type="button"
                  title="Eliminar"
                  aria-label="Eliminar cliente"
                  disabled={deleteMutation.isPending}
                  onClick={() => {
                    if (confirm("¿Eliminar este cliente?")) deleteMutation.mutate(customer.id);
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

      <CustomerFormModal
        title="Nuevo cliente"
        customer={null}
        isOpen={creating}
        saving={createMutation.isPending}
        onClose={() => setCreating(false)}
        onSave={(payload) => createMutation.mutate(payload)}
      />

      <CustomerFormModal
        title="Editar cliente"
        customer={editing}
        isOpen={!!editing}
        saving={updateMutation.isPending}
        onClose={() => setEditing(null)}
        onSave={(payload) => {
          if (editing) updateMutation.mutate({ id: editing.id, payload });
        }}
      />

      <CustomerDetailModal customer={viewing} onClose={() => setViewing(null)} />
    </PageWrapper>
  );
}

// ── Form modal (create + edit) ─────────────────────────────────────────────────

interface CustomerFormState {
  name: string;
  email: string;
  phone: string;
  telegram_username: string;
  notes: string;
}

function toFormState(customer: CustomerResponse | null): CustomerFormState {
  return {
    name: customer?.name ?? "",
    email: customer?.email ?? "",
    phone: customer?.phone ?? "",
    telegram_username: customer?.telegram_username ?? "",
    notes: customer?.notes ?? "",
  };
}

function CustomerFormModal({
  title,
  customer,
  isOpen,
  saving,
  onClose,
  onSave,
}: {
  title: string;
  customer: CustomerResponse | null;
  isOpen: boolean;
  saving: boolean;
  onClose: () => void;
  onSave: (payload: CreateCustomerPayload) => void;
}) {
  const [form, setForm] = useState<CustomerFormState>(() => toFormState(customer));
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (isOpen) {
      setForm(toFormState(customer));
      setError(null);
    }
  }, [isOpen, customer]);

  function set(key: keyof CustomerFormState) {
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
      telegram_username: form.telegram_username.trim() || null,
      notes: form.notes.trim() || null,
    });
  }

  return (
    <Modal isOpen={isOpen} onClose={onClose} title={title} size="lg">
      <form className="space-y-4" onSubmit={handleSubmit}>
        <Input
          label="Nombre"
          type="text"
          placeholder="Ej: Juan Pérez"
          value={form.name}
          onChange={set("name")}
          error={error ?? undefined}
        />
        <div className="grid grid-cols-2 gap-4">
          <Input
            label="Email (opcional)"
            type="email"
            placeholder="cliente@email.com"
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
          label="Usuario de Telegram (opcional)"
          type="text"
          placeholder="@usuario"
          value={form.telegram_username}
          onChange={set("telegram_username")}
        />
        <Input
          label="Notas (opcional)"
          type="text"
          placeholder="Ej: cliente mayorista, paga a 30 días..."
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

// ── Detail modal (ficha + ventas asociadas) ───────────────────────────────────

function CustomerDetailModal({
  customer,
  onClose,
}: {
  customer: CustomerResponse | null;
  onClose: () => void;
}) {
  const { data: sales = [], isLoading } = useQuery({
    queryKey: ["sales-by-customer", customer?.id],
    queryFn: () => salesService.getAllEntries({ customer_id: customer!.id }),
    enabled: !!customer,
    staleTime: 60 * 1000,
  });

  if (!customer) return null;

  const totalSales = sales.reduce((s: number, sale: SaleEntryResponse) => s + sale.amount, 0);

  return (
    <Modal isOpen={!!customer} onClose={onClose} title={customer.name} size="2xl">
      <div className="space-y-5">
        {/* Datos de contacto */}
        <section className="grid grid-cols-2 gap-3 text-sm">
          <div>
            <p className="text-xs text-vk-text-muted">Email</p>
            <p className="text-vk-text-primary">{customer.email?.trim() || "—"}</p>
          </div>
          <div>
            <p className="text-xs text-vk-text-muted">Teléfono</p>
            <p className="text-vk-text-primary">{customer.phone?.trim() || "—"}</p>
          </div>
          <div>
            <p className="text-xs text-vk-text-muted">Telegram</p>
            <p className="text-vk-text-primary">{customer.telegram_username?.trim() || "—"}</p>
          </div>
          <div>
            <p className="text-xs text-vk-text-muted">Estado</p>
            <p>
              {customer.is_active ? (
                <Badge variant="success">Activo</Badge>
              ) : (
                <Badge variant="default">Inactivo</Badge>
              )}
            </p>
          </div>
          {customer.notes?.trim() ? (
            <div className="col-span-2">
              <p className="text-xs text-vk-text-muted">Notas</p>
              <p className="text-vk-text-primary">{customer.notes}</p>
            </div>
          ) : null}
        </section>

        {/* Ventas asociadas */}
        <section className="space-y-2">
          <div className="flex items-center justify-between">
            <h3 className="font-display text-sm font-semibold text-vk-text-primary">
              Ventas asociadas
            </h3>
            {sales.length > 0 && (
              <span className="text-xs text-vk-text-muted">
                {sales.length} venta(s) · {formatARS(totalSales)}
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
          ) : sales.length === 0 ? (
            <p className="rounded-lg border border-vk-border-w bg-vk-surface-w px-4 py-3 text-sm text-vk-text-muted">
              Este cliente no tiene ventas asociadas todavía.
            </p>
          ) : (
            <div className="overflow-hidden rounded-lg border border-vk-border-w">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-vk-border-w bg-vk-bg-light text-left">
                    <th className="px-3 py-2 text-xs font-semibold text-vk-text-secondary">Fecha</th>
                    <th className="px-3 py-2 text-xs font-semibold text-vk-text-secondary">Monto</th>
                    <th className="px-3 py-2 text-xs font-semibold text-vk-text-secondary">Método de pago</th>
                  </tr>
                </thead>
                <tbody>
                  {sales.map((sale: SaleEntryResponse) => (
                    <tr key={sale.id} className="border-b border-vk-border-w last:border-b-0">
                      <td className="px-3 py-2 text-vk-text-primary">
                        {formatDateTime(sale.transaction_date)}
                      </td>
                      <td className="px-3 py-2 text-vk-text-primary">{formatARS(sale.amount)}</td>
                      <td className="px-3 py-2 text-vk-text-secondary">
                        {paymentLabel(sale.payment_method)}
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
