"use client";

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil, Trash2, Eye, Plus, FileText } from "lucide-react";
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
  type SupplierProductPurchase,
  type ReceiptLinePayload,
} from "@/services/suppliers.service";
import { expensesService, type ExpenseEntryResponse } from "@/services/expenses.service";
import { ContactCommunication } from "@/features/communication/ContactCommunication";
import { categoryDisplay } from "@/lib/expenseCategories";
import { formatDateTime } from "@/lib/datetime";
import { formatARS } from "@/features/dashboard/dashboardData";
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

const PAYMENT_METHOD_OPTIONS = Object.entries(PAYMENT_METHOD_LABELS).map(
  ([value, label]) => ({ value, label }),
);

function paymentLabel(method: string): string {
  return PAYMENT_METHOD_LABELS[method] ?? method;
}

/** Valida el formato XX-XXXXXXXX-X (con o sin guiones). Suave: solo cuando hay valor. */
function isValidCuil(value: string): boolean {
  return /^\d{2}-?\d{8}-?\d$/.test(value.trim());
}

/**
 * Normaliza un teléfono a dígitos para wa.me. wa.me requiere formato internacional
 * SIN "+". Si el número no trae código de país, se asume Argentina (54) — sin esto,
 * un teléfono local (ej. "11 1234-5678") generaría un link wa.me inválido.
 */
function whatsappDigits(phone: string): string {
  const digits = phone.replace(/[^\d]/g, "");
  if (!digits) return "";
  return digits.startsWith("54") ? digits : `54${digits}`;
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
  const [nameError, setNameError] = useState<string | null>(null);
  const [cuilError, setCuilError] = useState<string | null>(null);

  useEffect(() => {
    if (isOpen) {
      setForm(toFormState(supplier));
      setNameError(null);
      setCuilError(null);
    }
  }, [isOpen, supplier]);

  function set(key: keyof SupplierFormState) {
    return (e: React.ChangeEvent<HTMLInputElement>) =>
      setForm((prev) => ({ ...prev, [key]: e.target.value }));
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!form.name.trim()) {
      setNameError("El nombre o razón social es requerido.");
      return;
    }
    const cuil = form.cuil.trim();
    if (cuil && !isValidCuil(cuil)) {
      setCuilError("Formato de CUIL inválido (ej: 20-12345678-9).");
      return;
    }
    onSave({
      name: form.name.trim(),
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
        <div className="grid grid-cols-2 gap-4">
          <Input
            label="Nombre / Razón social"
            type="text"
            placeholder="Ej: Distribuidora del Sur"
            value={form.name}
            onChange={(e) => {
              set("name")(e);
              if (nameError) setNameError(null);
            }}
            error={nameError ?? undefined}
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
            label="CUIL / CUIT (opcional)"
            type="text"
            placeholder="20-12345678-9"
            value={form.cuil}
            onChange={(e) => {
              set("cuil")(e);
              if (cuilError) setCuilError(null);
            }}
            error={cuilError ?? undefined}
          />
          <Select
            label="Forma de pago (opcional)"
            placeholder="Sin especificar"
            options={PAYMENT_METHOD_OPTIONS}
            value={form.payment_method}
            onChange={(value) =>
              setForm((prev) => ({ ...prev, payment_method: value }))
            }
          />
        </div>
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
  const queryClient = useQueryClient();
  const toast = useToastStore((s) => s.add);
  const [uploadingReceipt, setUploadingReceipt] = useState(false);

  const { data: expenses = [], isLoading } = useQuery({
    queryKey: ["expenses-by-supplier", supplier?.id],
    queryFn: () => expensesService.getAllEntries({ supplier_id: supplier!.id }),
    enabled: !!supplier,
    staleTime: 60 * 1000,
  });

  const {
    data: products = [],
    isLoading: productsLoading,
    isError: productsError,
  } = useQuery({
    queryKey: ["supplier-products", supplier?.id],
    queryFn: () => suppliersService.getSupplierProducts(supplier!.id),
    enabled: !!supplier,
    staleTime: 60 * 1000,
  });

  if (!supplier) return null;

  const totalExpenses = expenses.reduce(
    (s: number, exp: ExpenseEntryResponse) => s + exp.amount,
    0,
  );

  const totalPurchased = products.reduce(
    (s: number, p: SupplierProductPurchase) => s + p.total_qty * p.unit_price,
    0,
  );

  const emailValue = supplier.email?.trim() ?? "";
  const phoneValue = supplier.phone?.trim() ?? "";
  const waDigits = whatsappDigits(phoneValue);

  return (
    <>
      <Modal isOpen={!!supplier} onClose={onClose} title={supplier.name} size="2xl">
        <div className="space-y-5">
          {/* Acciones de la ficha */}
          <div className="flex justify-end">
            <Button size="sm" variant="secondary" onClick={() => setUploadingReceipt(true)}>
              <FileText className="h-4 w-4" />
              Cargar remito
            </Button>
          </div>

          {/* Datos de contacto */}
          <section className="grid grid-cols-2 gap-3 text-sm">
            {supplier.last_name?.trim() ? (
              <div>
                <p className="text-xs text-vk-text-muted">Apellido</p>
                <p className="text-vk-text-primary">{supplier.last_name}</p>
              </div>
            ) : null}
            <div>
              <p className="text-xs text-vk-text-muted">CUIL / CUIT</p>
              <p className="text-vk-text-primary">{supplier.cuil?.trim() || "—"}</p>
            </div>
            <div>
              <p className="text-xs text-vk-text-muted">Forma de pago</p>
              <p className="text-vk-text-primary">
                {supplier.payment_method ? paymentLabel(supplier.payment_method) : "—"}
              </p>
            </div>
            <div>
              <p className="text-xs text-vk-text-muted">Email</p>
              {emailValue ? (
                <a
                  href={`mailto:${emailValue}`}
                  className="text-vk-blue underline-offset-2 hover:underline"
                >
                  {emailValue}
                </a>
              ) : (
                <p className="text-vk-text-primary">—</p>
              )}
            </div>
            <div>
              <p className="text-xs text-vk-text-muted">Teléfono</p>
              {phoneValue && waDigits ? (
                <a
                  href={`https://wa.me/${waDigits}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-vk-blue underline-offset-2 hover:underline"
                >
                  {phoneValue}
                </a>
              ) : (
                <p className="text-vk-text-primary">{phoneValue || "—"}</p>
              )}
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

          {/* Comunicación */}
          <ContactCommunication
            recipientType="supplier"
            recipientId={supplier.id}
            email={supplier.email}
            phone={supplier.phone}
          />

          {/* Productos comprados */}
          <section className="space-y-2">
            <div className="flex items-center justify-between">
              <h3 className="font-display text-sm font-semibold text-vk-text-primary">
                Productos comprados
              </h3>
              {products.length > 0 && (
                <span className="text-xs text-vk-text-muted">
                  Total comprado · {formatARS(totalPurchased)}
                </span>
              )}
            </div>

            {productsLoading ? (
              <div className="space-y-2">
                {[...Array<number>(3)].map((_, i) => (
                  <div
                    key={i}
                    className="h-9 animate-pulse rounded-lg border border-vk-border-w bg-vk-surface-w"
                  />
                ))}
              </div>
            ) : productsError ? (
              <p className="rounded-lg border border-vk-danger/20 bg-vk-danger-bg px-4 py-3 text-sm text-vk-danger">
                Error al cargar los productos comprados.
              </p>
            ) : products.length === 0 ? (
              <p className="rounded-lg border border-vk-border-w bg-vk-surface-w px-4 py-3 text-sm text-vk-text-muted">
                Sin compras registradas.
              </p>
            ) : (
              <div className="overflow-hidden rounded-lg border border-vk-border-w">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-vk-border-w bg-vk-bg-light text-left">
                      <th className="px-3 py-2 text-xs font-semibold text-vk-text-secondary">Producto</th>
                      <th className="px-3 py-2 text-xs font-semibold text-vk-text-secondary">Última compra</th>
                      <th className="px-3 py-2 text-xs font-semibold text-vk-text-secondary">Cantidad</th>
                      <th className="px-3 py-2 text-xs font-semibold text-vk-text-secondary">Precio unit.</th>
                    </tr>
                  </thead>
                  <tbody>
                    {products.map((p: SupplierProductPurchase) => (
                      <tr
                        key={p.product_id}
                        className="border-b border-vk-border-w last:border-b-0"
                      >
                        <td className="px-3 py-2 text-vk-text-primary">{p.name}</td>
                        <td className="px-3 py-2 text-vk-text-secondary">
                          {formatDateTime(p.last_purchase_at)}
                        </td>
                        <td className="px-3 py-2 text-vk-text-primary">{p.total_qty}</td>
                        <td className="px-3 py-2 text-vk-text-primary">
                          {formatARS(p.unit_price)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          {/* Compras asociadas (gastos) */}
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

      <ReceiptModal
        supplier={supplier}
        isOpen={uploadingReceipt}
        onClose={() => setUploadingReceipt(false)}
        onUploaded={async () => {
          setUploadingReceipt(false);
          toast("Remito cargado.", "success");
          await Promise.all([
            queryClient.invalidateQueries({ queryKey: ["supplier-products", supplier.id] }),
            queryClient.invalidateQueries({ queryKey: ["expenses-by-supplier", supplier.id] }),
            queryClient.invalidateQueries({ queryKey: ["expenses-entries"] }),
            queryClient.invalidateQueries({ queryKey: ["expenses-all"] }),
            queryClient.invalidateQueries({ queryKey: ["products-list"] }),
            queryClient.invalidateQueries({ queryKey: ["products"] }),
            queryClient.invalidateQueries({ queryKey: ["inventory"] }),
          ]);
          void queryClient.invalidateQueries({ queryKey: ["health-scores"] });
        }}
      />
    </>
  );
}

// ── Receipt modal (Fase 4 — cargar remito) ────────────────────────────────────

interface ReceiptLineForm {
  product_name: string;
  sku: string;
  qty: string;
  unit_price: string;
}

function emptyReceiptLine(): ReceiptLineForm {
  return { product_name: "", sku: "", qty: "", unit_price: "" };
}

function ReceiptModal({
  supplier,
  isOpen,
  onClose,
  onUploaded,
}: {
  supplier: SupplierResponse;
  isOpen: boolean;
  onClose: () => void;
  onUploaded: () => void | Promise<void>;
}) {
  const toast = useToastStore((s) => s.add);
  const [lines, setLines] = useState<ReceiptLineForm[]>(() => [emptyReceiptLine()]);
  const [shipping, setShipping] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (isOpen) {
      setLines([emptyReceiptLine()]);
      setShipping("");
      setError(null);
    }
  }, [isOpen]);

  const idempotencyKey = useMemo(
    () => (typeof crypto !== "undefined" ? crypto.randomUUID() : `${Date.now()}`),
    // Regenerate per open so retries after a failure get a fresh key.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [isOpen],
  );

  const uploadMutation = useMutation({
    mutationFn: (payload: { lines: ReceiptLinePayload[]; shipping_cost?: number }) =>
      suppliersService.uploadReceipt(
        supplier.id,
        { ...payload, currency: "ARS" },
        idempotencyKey,
      ),
    onSuccess: () => {
      void onUploaded();
    },
    onError: () => toast("No se pudo cargar el remito.", "error"),
  });

  function setLine(index: number, key: keyof ReceiptLineForm, value: string) {
    setLines((prev) =>
      prev.map((line, i) => (i === index ? { ...line, [key]: value } : line)),
    );
    if (error) setError(null);
  }

  function addLine() {
    setLines((prev) => [...prev, emptyReceiptLine()]);
  }

  function removeLine(index: number) {
    setLines((prev) => (prev.length > 1 ? prev.filter((_, i) => i !== index) : prev));
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();

    const parsed: ReceiptLinePayload[] = [];
    for (const line of lines) {
      const name = line.product_name.trim();
      if (!name) {
        setError("Cada línea necesita un producto.");
        return;
      }
      const qty = Number(line.qty);
      if (!Number.isFinite(qty) || qty <= 0) {
        setError(`Cantidad inválida en "${name}". Debe ser mayor a 0.`);
        return;
      }
      if (!Number.isInteger(qty)) {
        // El backend trabaja con unidades enteras (stock/COGS) y rechaza fraccionarios.
        setError(`La cantidad en "${name}" debe ser un número entero de unidades.`);
        return;
      }
      const unitPrice = Number(line.unit_price);
      if (!Number.isFinite(unitPrice) || unitPrice < 0) {
        setError(`Precio unitario inválido en "${name}".`);
        return;
      }
      const sku = line.sku.trim();
      parsed.push({
        product_name: name,
        qty,
        unit_price: unitPrice,
        ...(sku ? { sku } : {}),
      });
    }

    if (parsed.length === 0) {
      setError("Agregá al menos una línea.");
      return;
    }

    let shippingCost: number | undefined;
    if (shipping.trim()) {
      const value = Number(shipping);
      if (!Number.isFinite(value) || value < 0) {
        setError("El costo de envío no puede ser negativo.");
        return;
      }
      shippingCost = value;
    }

    uploadMutation.mutate({
      lines: parsed,
      ...(shippingCost !== undefined ? { shipping_cost: shippingCost } : {}),
    });
  }

  return (
    <Modal isOpen={isOpen} onClose={onClose} title={`Cargar remito — ${supplier.name}`} size="2xl">
      <form className="space-y-4" onSubmit={handleSubmit}>
        <p className="text-xs text-vk-text-muted">
          Los montos se registran en pesos argentinos (ARS).
        </p>

        <div className="space-y-2">
          <div className="grid grid-cols-[1fr_0.8fr_0.6fr_0.7fr_auto] gap-2 px-1 text-xs font-semibold text-vk-text-secondary">
            <span>Producto</span>
            <span>SKU (opcional)</span>
            <span>Cantidad</span>
            <span>Precio unit.</span>
            <span className="sr-only">Quitar</span>
          </div>

          {lines.map((line, index) => (
            <div
              key={index}
              className="grid grid-cols-[1fr_0.8fr_0.6fr_0.7fr_auto] items-start gap-2"
            >
              <Input
                type="text"
                placeholder="Ej: Yerba 1kg"
                value={line.product_name}
                onChange={(e) => setLine(index, "product_name", e.target.value)}
              />
              <Input
                type="text"
                placeholder="—"
                value={line.sku}
                onChange={(e) => setLine(index, "sku", e.target.value)}
              />
              <Input
                type="number"
                min={0}
                step="any"
                placeholder="0"
                value={line.qty}
                onChange={(e) => setLine(index, "qty", e.target.value)}
              />
              <Input
                type="number"
                min={0}
                step="any"
                placeholder="0"
                value={line.unit_price}
                onChange={(e) => setLine(index, "unit_price", e.target.value)}
              />
              <button
                type="button"
                title="Quitar línea"
                aria-label="Quitar línea"
                disabled={lines.length === 1}
                onClick={() => removeLine(index)}
                className="mt-0.5 inline-flex h-10 w-10 items-center justify-center rounded border border-vk-danger/30 text-vk-danger transition-colors hover:bg-vk-danger-bg disabled:opacity-40"
              >
                <Trash2 className="h-4 w-4" />
              </button>
            </div>
          ))}

          <Button type="button" variant="secondary" size="sm" onClick={addLine}>
            <Plus className="h-4 w-4" />
            Agregar línea
          </Button>
        </div>

        <div className="max-w-xs">
          <Input
            label="Costo de envío (opcional)"
            type="number"
            min={0}
            step="any"
            placeholder="0"
            value={shipping}
            onChange={(e) => {
              setShipping(e.target.value);
              if (error) setError(null);
            }}
            hint="Se registra como gasto de logística."
          />
        </div>

        {error ? <p className="text-xs text-vk-danger">{error}</p> : null}

        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" variant="secondary" size="sm" onClick={onClose}>
            Cancelar
          </Button>
          <Button type="submit" size="sm" loading={uploadMutation.isPending}>
            Guardar remito
          </Button>
        </div>
      </form>
    </Modal>
  );
}
