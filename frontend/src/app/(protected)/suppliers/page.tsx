"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil, Trash2 } from "lucide-react";
import { PageWrapper } from "@/components/layout/PageWrapper";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Badge } from "@/components/ui/Badge";
import { Modal } from "@/components/ui/Modal";
import { Select } from "@/components/ui/Select";
import { EmptyState } from "@/components/ui/EmptyState";
import { SmartTable } from "@/components/ui/SmartTable";
import {
  suppliersService,
  type CreateSupplierPayload,
  type SupplierResponse,
} from "@/services/suppliers.service";
import {
  PAYMENT_METHOD_OPTIONS,
  isValidCuil,
  paymentLabel,
} from "@/lib/suppliers";
import { fieldDefinitionsService } from "@/services/fieldDefinitions.service";
import { buildEditableCustomFieldColumns } from "@/lib/customFieldsEditable";
import { AddColumnButton } from "@/features/customFields/AddColumnButton";
import { formatDateTime } from "@/lib/datetime";
import { useToastStore } from "@/stores/toastStore";

const COLUMNS = [
  {
    key: "name",
    header: "Nombre / Razón social",
    hideable: true,
    render: (v: unknown, row: Record<string, unknown>) => (
      <Link
        href={`/suppliers/${(row as unknown as SupplierResponse).id}`}
        className="font-medium text-vk-text-primary underline-offset-2 hover:text-vk-blue hover:underline"
      >
        {String(v)}
      </Link>
    ),
    csvValue: (v: unknown) => String(v ?? ""),
  },
  {
    key: "last_name",
    header: "Apellido",
    hideable: true,
    render: (v: unknown) => String(v ?? "").trim() || "—",
    csvValue: (v: unknown) => String(v ?? "").trim(),
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
    key: "cuil",
    header: "CUIL",
    hideable: true,
    render: (v: unknown) => String(v ?? "").trim() || "—",
    csvValue: (v: unknown) => String(v ?? "").trim(),
  },
  {
    key: "payment_method",
    header: "Forma de pago",
    hideable: true,
    render: (v: unknown) => {
      const s = String(v ?? "").trim();
      return s ? paymentLabel(s) : "—";
    },
    csvValue: (v: unknown) => {
      const s = String(v ?? "").trim();
      return s ? paymentLabel(s) : "";
    },
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
  const queryClient = useQueryClient();
  const toast = useToastStore((s) => s.add);

  const { data: suppliers = [], isLoading, isError } = useQuery({
    queryKey: ["suppliers-list"],
    queryFn: () => suppliersService.getAllSuppliers(),
    staleTime: 2 * 60 * 1000,
  });

  const { data: fieldDefs = [] } = useQuery({
    queryKey: ["field-definitions", "supplier"],
    queryFn: () => fieldDefinitionsService.getAll("supplier"),
    staleTime: 5 * 60 * 1000,
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

  const saveSupplierCustomField = async (
    row: Record<string, unknown>,
    custom_fields: Record<string, unknown>,
  ) => {
    try {
      await suppliersService.updateSupplier(String(row.id), { custom_fields });
      await queryClient.invalidateQueries({ queryKey: ["suppliers-list"] });
    } catch {
      toast("No se pudo guardar el dato.", "error");
    }
  };

  const columns = [
    ...COLUMNS,
    ...buildEditableCustomFieldColumns<Record<string, unknown>>(
      fieldDefs,
      saveSupplierCustomField,
    ),
  ];

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
          columns={columns}
          data={tableData as Record<string, unknown>[]}
          exportFilename="vektor-proveedores"
          toolbarActions={<AddColumnButton entityType="supplier" entityLabel="Proveedores" />}
          renderActions={(row) => {
            const supplier = row as unknown as SupplierResponse;
            return (
              <div className="flex items-center gap-1">
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
    </PageWrapper>
  );
}

// ── Form modal (create + edit) ─────────────────────────────────────────────────

interface SupplierFormState {
  name: string;
  last_name: string;
  cuil: string;
  payment_method: string;
  email: string;
  phone: string;
  notes: string;
}

function toFormState(supplier: SupplierResponse | null): SupplierFormState {
  return {
    name: supplier?.name ?? "",
    last_name: supplier?.last_name ?? "",
    cuil: supplier?.cuil ?? "",
    payment_method: supplier?.payment_method ?? "",
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

  // El sentinela "No identificado" es un placeholder (agrupa compras sin proveedor):
  // no se le exigen los datos de contacto. Para cualquier otro proveedor son
  // obligatorios — solo el apellido queda opcional.
  const isSentinel =
    !!supplier &&
    (supplier.name === "No identificado" ||
      supplier.custom_fields?._sentinel === true ||
      supplier.custom_fields?._sentinel === "true");

  useEffect(() => {
    if (isOpen) {
      setForm(toFormState(supplier));
      setError(null);
    }
  }, [isOpen, supplier]);

  function set(key: keyof SupplierFormState) {
    return (e: React.ChangeEvent<HTMLInputElement>) => {
      setForm((prev) => ({ ...prev, [key]: e.target.value }));
      if (error) setError(null);
    };
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const name = form.name.trim();
    const cuil = form.cuil.trim();
    if (!name) {
      setError("El nombre o razón social es requerido.");
      return;
    }
    if (!isSentinel) {
      if (!form.email.trim()) return setError("El email es requerido.");
      if (!form.phone.trim()) return setError("El teléfono es requerido.");
      if (!cuil) return setError("El CUIL es requerido.");
      if (!form.payment_method) return setError("La forma de pago es requerida.");
    }
    if (cuil && !isValidCuil(cuil)) {
      setError("CUIL inválido: revisá el número (dígito verificador no coincide).");
      return;
    }
    onSave({
      name,
      last_name: form.last_name.trim() || null,
      cuil: cuil || null,
      payment_method: form.payment_method || null,
      email: form.email.trim() || null,
      phone: form.phone.trim() || null,
      notes: form.notes.trim() || null,
    });
  }

  return (
    <Modal isOpen={isOpen} onClose={onClose} title={title} size="lg">
      <form className="space-y-4" onSubmit={handleSubmit}>
        {error && (
          <p className="rounded-lg border border-vk-danger/30 bg-vk-danger-bg px-3 py-2 text-sm text-vk-danger">
            {error}
          </p>
        )}
        {!isSentinel && (
          <p className="text-xs text-vk-text-secondary">
            Los campos sin “(opcional)” son obligatorios.
          </p>
        )}
        <div className="grid grid-cols-2 gap-4">
          <Input
            label="Nombre / Razón social"
            type="text"
            placeholder="Ej: Distribuidora del Sur"
            value={form.name}
            onChange={set("name")}
          />
          <Input
            label="Apellido (opcional)"
            type="text"
            placeholder="Para personas (vacío si es empresa)"
            value={form.last_name}
            onChange={set("last_name")}
          />
        </div>
        <div className="grid grid-cols-2 gap-4">
          <Input
            label="CUIL / CUIT"
            type="text"
            placeholder="20-12345678-6"
            value={form.cuil}
            onChange={set("cuil")}
          />
          <Select
            label="Forma de pago"
            placeholder="Elegí una opción"
            options={PAYMENT_METHOD_OPTIONS}
            value={form.payment_method}
            onChange={(value) => {
              setForm((prev) => ({ ...prev, payment_method: value }));
              if (error) setError(null);
            }}
          />
        </div>
        <div className="grid grid-cols-2 gap-4">
          <Input
            label="Email"
            type="email"
            placeholder="proveedor@email.com"
            value={form.email}
            onChange={set("email")}
          />
          <Input
            label="Teléfono"
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
