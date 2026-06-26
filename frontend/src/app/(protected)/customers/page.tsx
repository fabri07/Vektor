"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil, Trash2, Eye } from "lucide-react";
import { PageWrapper } from "@/components/layout/PageWrapper";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { Badge } from "@/components/ui/Badge";
import { Modal } from "@/components/ui/Modal";
import { EmptyState } from "@/components/ui/EmptyState";
import { SmartTable } from "@/components/ui/SmartTable";
import {
  customersService,
  type CreateCustomerPayload,
  type CustomerResponse,
  type CustomerExtractionResponse,
} from "@/services/customers.service";
import {
  IVA_CONDITION_OPTIONS,
  customerTypeLabel,
  ivaConditionLabel,
  isValidCuit,
  isValidDni,
} from "@/lib/fiscal";
import { CustomerFileModal } from "@/features/customers/CustomerFileModal";
import { fieldDefinitionsService } from "@/services/fieldDefinitions.service";
import { buildEditableCustomFieldColumns } from "@/lib/customFieldsEditable";
import { AddColumnButton } from "@/features/customFields/AddColumnButton";
import { useSaveCustomField } from "@/features/customFields/useSaveCustomField";
import { formatDateTime } from "@/lib/datetime";
import { useToastStore } from "@/stores/toastStore";

function customerDoc(c: CustomerResponse): string {
  if (c.cuit?.trim()) return `CUIT ${c.cuit.trim()}`;
  if (c.dni?.trim()) return `DNI ${c.dni.trim()}`;
  return "—";
}

const COLUMNS = [
  {
    key: "name",
    header: "Nombre / Razón social",
    hideable: true,
    render: (v: unknown, row: Record<string, unknown>) => {
      const c = row as unknown as CustomerResponse;
      return (
        <span className="flex items-center gap-2">
          <Link
            href={`/customers/${c.id}`}
            className="font-medium text-vk-blue underline-offset-2 hover:underline"
          >
            {String(v)}
          </Link>
          {c.is_sentinel && (
            <span title="Agrupa ventas donde no se identificó cliente.">
              <Badge variant="default">Ventas sin cliente</Badge>
            </span>
          )}
        </span>
      );
    },
    csvValue: (v: unknown) => String(v ?? ""),
  },
  {
    key: "customer_type",
    header: "Tipo",
    hideable: true,
    render: (v: unknown) => customerTypeLabel(v as string | null),
    csvValue: (v: unknown) => customerTypeLabel(v as string | null),
  },
  {
    key: "last_name",
    header: "Apellido",
    hideable: true,
    defaultVisible: false,
    render: (v: unknown) => String(v ?? "").trim() || "—",
    csvValue: (v: unknown) => String(v ?? "").trim(),
  },
  {
    key: "_doc",
    header: "CUIT / DNI",
    hideable: true,
    render: (_: unknown, row: Record<string, unknown>) =>
      customerDoc(row as unknown as CustomerResponse),
    csvValue: (_: unknown, row: Record<string, unknown>) =>
      customerDoc(row as unknown as CustomerResponse),
  },
  {
    key: "iva_condition",
    header: "Condición IVA",
    hideable: true,
    render: (v: unknown) => ivaConditionLabel(v as string | null),
    csvValue: (v: unknown) => ivaConditionLabel(v as string | null),
  },
  {
    key: "phone",
    header: "Celular",
    hideable: true,
    render: (v: unknown) => String(v ?? "").trim() || "—",
    csvValue: (v: unknown) => String(v ?? "").trim(),
  },
  {
    key: "email",
    header: "Email",
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
  const [fileOpen, setFileOpen] = useState(false);
  // Sugerencia de una ficha leída por archivo: prellena el form de alta (no persiste).
  const [prefill, setPrefill] = useState<CustomerExtractionResponse | null>(null);
  const queryClient = useQueryClient();
  const toast = useToastStore((s) => s.add);

  const { data: customers = [], isLoading, isError } = useQuery({
    queryKey: ["customers-list"],
    // include_sentinel: muestra el centinela "Local" (ventas sin cliente) si existe.
    queryFn: () => customersService.getAllCustomers({ include_sentinel: true }),
    staleTime: 2 * 60 * 1000,
  });

  const { data: fieldDefs = [] } = useQuery({
    queryKey: ["field-definitions", "customer"],
    queryFn: () => fieldDefinitionsService.getAll("customer"),
    staleTime: 5 * 60 * 1000,
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

  const tableData = customers.map((c) => ({ ...c, _status: null, _doc: null }));

  const saveCustomerCustomField = useSaveCustomField({
    listKey: ["customers-list"],
    update: (id, custom_fields) => customersService.updateCustomer(id, { custom_fields }),
  });

  const columns = [
    ...COLUMNS,
    ...buildEditableCustomFieldColumns<Record<string, unknown>>(
      fieldDefs,
      saveCustomerCustomField,
      // El centinela "Local" no es editable (el back da 400): celdas read-only.
      (row) => (row as unknown as CustomerResponse).is_sentinel,
    ),
  ];

  return (
    <PageWrapper
      title="Clientes"
      actions={
        <div className="flex items-center gap-2">
          <Button size="sm" variant="secondary" onClick={() => setFileOpen(true)}>
            Cargar desde archivo
          </Button>
          <Button
            size="sm"
            onClick={() => {
              setPrefill(null);
              setCreating(true);
            }}
          >
            Nuevo cliente
          </Button>
        </div>
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
          columns={columns}
          data={tableData as Record<string, unknown>[]}
          exportFilename="vektor-clientes"
          toolbarActions={<AddColumnButton entityType="customer" entityLabel="Clientes" />}
          renderActions={(row) => {
            const customer = row as unknown as CustomerResponse;
            return (
              <div className="flex items-center gap-1">
                <Link
                  href={`/customers/${customer.id}`}
                  title="Ver"
                  aria-label="Ver cliente"
                  className="inline-flex h-8 w-8 items-center justify-center rounded border border-vk-border-w text-vk-text-secondary transition-colors hover:bg-vk-bg-light hover:text-vk-text-primary"
                >
                  <Eye className="h-4 w-4" />
                </Link>
                {/* El centinela "Local" no se edita ni elimina (backend 400). */}
                {!customer.is_sentinel && (
                  <>
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
                  </>
                )}
              </div>
            );
          }}
        />
      )}

      <CustomerFormModal
        title="Nuevo cliente"
        customer={null}
        prefill={prefill}
        isOpen={creating}
        saving={createMutation.isPending}
        onClose={() => {
          setCreating(false);
          setPrefill(null);
        }}
        onSave={(payload) => createMutation.mutate(payload)}
      />

      <CustomerFileModal
        isOpen={fileOpen}
        onClose={() => setFileOpen(false)}
        onExtracted={(extraction) => {
          // La lectura solo sugiere: cerramos el modal de archivo y abrimos el alta prellenada.
          setFileOpen(false);
          setPrefill(extraction);
          setCreating(true);
          const notes: string[] = [];
          if (extraction.confidence === "LOW") {
            notes.push("Confianza baja: revisá bien los datos antes de guardar.");
          }
          notes.push(...extraction.warnings);
          if (notes.length) toast(notes.join(" "), "info");
        }}
        onImported={() =>
          queryClient.invalidateQueries({ queryKey: ["customers-list"] })
        }
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
    </PageWrapper>
  );
}

// ── Form modal (create + edit) ─────────────────────────────────────────────────

type CustomerType = "person" | "company";

interface CustomerFormState {
  customer_type: CustomerType;
  name: string;
  last_name: string;
  dni: string;
  cuit: string;
  iva_condition: string;
  phone: string;
  email: string;
  address: string;
  locality: string;
  province: string;
  postal_code: string;
  birthday: string;
  telegram_username: string;
  notes: string;
}

function toFormState(customer: CustomerResponse | null): CustomerFormState {
  return {
    customer_type: (customer?.customer_type as CustomerType) ?? "person",
    name: customer?.name ?? "",
    last_name: customer?.last_name ?? "",
    dni: customer?.dni ?? "",
    cuit: customer?.cuit ?? "",
    iva_condition: customer?.iva_condition ?? "",
    phone: customer?.phone ?? "",
    email: customer?.email ?? "",
    address: customer?.address ?? "",
    locality: customer?.locality ?? "",
    province: customer?.province ?? "",
    postal_code: customer?.postal_code ?? "",
    birthday: customer?.birthday ?? "",
    telegram_username: customer?.telegram_username ?? "",
    notes: customer?.notes ?? "",
  };
}

// Sugerencia de una ficha leída por archivo → estado inicial del form de alta.
function prefillToFormState(p: CustomerExtractionResponse): CustomerFormState {
  return {
    customer_type: (p.customer_type as CustomerType) ?? "person",
    name: p.name ?? "",
    last_name: p.last_name ?? "",
    dni: p.dni ?? "",
    cuit: p.cuit ?? "",
    iva_condition: p.iva_condition ?? "",
    phone: p.phone ?? "",
    email: p.email ?? "",
    address: p.address ?? "",
    locality: p.locality ?? "",
    province: p.province ?? "",
    postal_code: p.postal_code ?? "",
    birthday: p.birthday ?? "",
    telegram_username: "",
    notes: "",
  };
}

function CustomerFormModal({
  title,
  customer,
  prefill,
  isOpen,
  saving,
  onClose,
  onSave,
}: {
  title: string;
  customer: CustomerResponse | null;
  prefill?: CustomerExtractionResponse | null;
  isOpen: boolean;
  saving: boolean;
  onClose: () => void;
  onSave: (payload: CreateCustomerPayload) => void;
}) {
  const [form, setForm] = useState<CustomerFormState>(() => toFormState(customer));
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (isOpen) {
      // En alta, una sugerencia leída por archivo tiene prioridad sobre el form vacío.
      setForm(
        !customer && prefill ? prefillToFormState(prefill) : toFormState(customer),
      );
      setError(null);
    }
  }, [isOpen, customer, prefill]);

  const isCompany = form.customer_type === "company";

  function set(key: keyof CustomerFormState) {
    return (e: React.ChangeEvent<HTMLInputElement>) =>
      setForm((prev) => ({ ...prev, [key]: e.target.value }));
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const name = form.name.trim();
    const lastName = form.last_name.trim();
    const dni = form.dni.trim();
    const cuit = form.cuit.trim();
    const phone = form.phone.trim();

    if (!name) {
      setError(isCompany ? "La razón social es requerida." : "El nombre es requerido.");
      return;
    }
    if (!isCompany && !lastName) {
      setError("El apellido es requerido para una persona.");
      return;
    }
    if (!dni && !cuit) {
      setError("Cargá al menos un documento: DNI o CUIT.");
      return;
    }
    if (cuit && !isValidCuit(cuit)) {
      setError("El CUIT/CUIL no es válido (revisá el dígito verificador).");
      return;
    }
    if (dni && !isValidDni(dni)) {
      setError("El DNI no es válido (7 u 8 dígitos).");
      return;
    }
    if (!phone) {
      setError("El celular es requerido.");
      return;
    }

    // doc_type prioriza CUIT (fiscalmente más completo) cuando ambos están cargados.
    const docType = cuit ? "cuit" : dni ? "dni" : null;

    onSave({
      customer_type: form.customer_type,
      name,
      last_name: isCompany ? null : lastName || null,
      doc_type: docType,
      dni: dni || null,
      cuit: cuit || null,
      iva_condition: form.iva_condition || null,
      phone,
      email: form.email.trim() || null,
      address: form.address.trim() || null,
      locality: form.locality.trim() || null,
      province: form.province.trim() || null,
      postal_code: form.postal_code.trim() || null,
      birthday: form.birthday.trim() || null,
      telegram_username: form.telegram_username.trim() || null,
      notes: form.notes.trim() || null,
    });
  }

  return (
    <Modal isOpen={isOpen} onClose={onClose} title={title} size="lg">
      <form className="space-y-4" onSubmit={handleSubmit}>
        {/* Tipo de cliente */}
        <Select
          label="Tipo de cliente"
          options={[
            { value: "person", label: "Persona" },
            { value: "company", label: "Empresa" },
          ]}
          value={form.customer_type}
          onChange={(v) =>
            setForm((prev) => ({ ...prev, customer_type: v as CustomerType }))
          }
        />

        {/* Identidad */}
        <div className="grid grid-cols-2 gap-4">
          <Input
            label={isCompany ? "Razón social" : "Nombre"}
            type="text"
            placeholder={isCompany ? "Distribuidora SA" : "Juan"}
            value={form.name}
            onChange={set("name")}
          />
          {!isCompany && (
            <Input
              label="Apellido"
              type="text"
              placeholder="Pérez"
              value={form.last_name}
              onChange={set("last_name")}
            />
          )}
        </div>

        {/* Documento fiscal */}
        <div className="grid grid-cols-2 gap-4">
          {!isCompany && (
            <Input
              label="DNI (opcional si cargás CUIT)"
              type="text"
              placeholder="30123456"
              value={form.dni}
              onChange={set("dni")}
            />
          )}
          <Input
            label="CUIT / CUIL"
            type="text"
            placeholder="20-12345678-6"
            value={form.cuit}
            onChange={set("cuit")}
          />
        </div>

        {/* Condición IVA */}
        <Select
          label="Condición frente al IVA (opcional)"
          placeholder="Seleccioná una condición"
          options={IVA_CONDITION_OPTIONS}
          value={form.iva_condition}
          onChange={(v) => setForm((prev) => ({ ...prev, iva_condition: v }))}
        />

        {/* Contacto */}
        <div className="grid grid-cols-2 gap-4">
          <Input
            label="Celular (WhatsApp)"
            type="text"
            placeholder="+54 9 11 1234-5678"
            value={form.phone}
            onChange={set("phone")}
          />
          <Input
            label="Email (opcional)"
            type="email"
            placeholder="cliente@email.com"
            value={form.email}
            onChange={set("email")}
          />
        </div>

        {/* Domicilio de entrega */}
        <Input
          label="Dirección de entrega (opcional)"
          type="text"
          placeholder="Calle y número"
          value={form.address}
          onChange={set("address")}
        />
        <div className="grid grid-cols-3 gap-4">
          <Input
            label="Localidad (opcional)"
            type="text"
            placeholder="Quilmes"
            value={form.locality}
            onChange={set("locality")}
          />
          <Input
            label="Provincia (opcional)"
            type="text"
            placeholder="Buenos Aires"
            value={form.province}
            onChange={set("province")}
          />
          <Input
            label="Cód. postal (opcional)"
            type="text"
            placeholder="1878"
            value={form.postal_code}
            onChange={set("postal_code")}
          />
        </div>

        {/* Comerciales */}
        <div className="grid grid-cols-2 gap-4">
          <Input
            label="Cumpleaños (opcional)"
            type="date"
            value={form.birthday}
            onChange={set("birthday")}
          />
          <Input
            label="Usuario de Telegram (opcional)"
            type="text"
            placeholder="@usuario"
            value={form.telegram_username}
            onChange={set("telegram_username")}
          />
        </div>
        <Input
          label="Notas / Preferencias (opcional)"
          type="text"
          placeholder="Ej: prefiere entregas a la tarde, compra mayorista..."
          value={form.notes}
          onChange={set("notes")}
        />

        {error && (
          <p className="rounded-lg border border-vk-danger/20 bg-vk-danger-bg px-3 py-2 text-sm text-vk-danger">
            {error}
          </p>
        )}

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
