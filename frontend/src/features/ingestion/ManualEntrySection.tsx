"use client";

import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { productsService } from "@/services/products.service";
import { customersService } from "@/services/customers.service";
import { suppliersService } from "@/services/suppliers.service";
import { fieldDefinitionsService } from "@/services/fieldDefinitions.service";
import { ALL_CATEGORIES, CATEGORY_LABELS } from "@/lib/expenseCategories";
import type { FieldDefinition } from "@/types/api";
import { CustomFieldsForm } from "./CustomFieldsForm";
import {
  useOfflineQueueCount,
  useOfflineSubmit,
  useOnlineStatus,
} from "./useOfflineSubmit";

// ── helpers ──────────────────────────────────────────────────────────────────

// "YYYY-MM-DDTHH:mm" en hora local, para inputs datetime-local (captura la hora real).
function nowStr(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}`
  );
}

// Quita valores vacíos/undefined del objeto de custom fields antes de enviarlo.
function cleanCustom(values: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(values).filter(([, v]) => v !== undefined && v !== null && v !== ""),
  );
}

// Valida los custom fields requeridos del vertical (degradación limpia si no hay defs).
function validateRequiredCustom(
  defs: FieldDefinition[] | undefined,
  values: Record<string, unknown>,
): Record<string, string> {
  const errs: Record<string, string> = {};
  for (const d of defs ?? []) {
    if (!d.is_required) continue;
    const v = values[d.field_key];
    if (v === undefined || v === null || v === "") errs[d.field_key] = "Requerido.";
  }
  return errs;
}

// ── Toast ─────────────────────────────────────────────────────────────────────

type ToastState = { type: "success" | "error"; message: string } | null;

function Toast({ toast }: { toast: ToastState }) {
  if (!toast) return null;
  return (
    <div
      className={[
        "rounded-lg border px-4 py-2.5 text-sm",
        toast.type === "success"
          ? "border-vk-success/20 bg-vk-success-bg text-vk-success"
          : "border-vk-danger/20 bg-vk-danger-bg text-vk-danger",
      ].join(" ")}
    >
      {toast.message}
    </div>
  );
}

// ── Shared select styles ──────────────────────────────────────────────────────

const selectClass =
  "h-9 w-full rounded-lg border border-vk-border-w bg-vk-surface-w px-3 text-sm text-vk-text-primary focus:outline-none focus:ring-2 focus:ring-vk-blue/15 focus:border-vk-blue/40 disabled:opacity-40";

const PAYMENT_METHODS = [
  { value: "cash", label: "Efectivo" },
  { value: "debit_card", label: "Tarjeta débito" },
  { value: "credit_card", label: "Tarjeta crédito" },
  { value: "transfer", label: "Transferencia" },
  { value: "qr", label: "QR / Mercado Pago" },
  { value: "account", label: "Cuenta corriente (fiado)" },
  { value: "other", label: "Otro" },
];

const EXPENSE_TYPES = [
  { value: "OPEX", label: "Gasto operativo (OPEX)" },
  { value: "COGS", label: "Compra de mercadería (COGS)" },
];

// ── Sale form ─────────────────────────────────────────────────────────────────

interface SaleForm {
  amount: string;
  quantity: string;
  transaction_date: string;
  payment_method: string;
  product_id: string;
  customer_id: string;
  notes: string;
}

function emptySaleForm(): SaleForm {
  return {
    amount: "",
    quantity: "1",
    transaction_date: nowStr(),
    payment_method: "cash",
    product_id: "",
    customer_id: "",
    notes: "",
  };
}

function SaleTab({ onToast }: { onToast: (t: ToastState) => void }) {
  const { submit } = useOfflineSubmit();
  const [submitting, setSubmitting] = useState(false);
  const [form, setForm] = useState<SaleForm>(emptySaleForm);
  const [errors, setErrors] = useState<Partial<SaleForm>>({});
  const [customValues, setCustomValues] = useState<Record<string, unknown>>({});
  const [customErrors, setCustomErrors] = useState<Record<string, string>>({});

  const { data: products } = useQuery({
    queryKey: ["products-list"],
    queryFn: () => productsService.getAllProducts({ is_active: true }),
  });
  const { data: customers } = useQuery({
    queryKey: ["customers-list"],
    queryFn: () => customersService.getAllCustomers(),
  });
  const { data: customDefs } = useQuery({
    queryKey: ["field-definitions", "sale"],
    queryFn: () => fieldDefinitionsService.getAll("sale"),
  });

  function resetForm() {
    setForm(emptySaleForm());
    setErrors({});
    setCustomValues({});
    setCustomErrors({});
  }

  function set(key: keyof SaleForm) {
    return (
      e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>,
    ) => setForm((prev) => ({ ...prev, [key]: e.target.value }));
  }

  function setCustom(fieldKey: string, value: unknown) {
    setCustomValues((prev) => ({ ...prev, [fieldKey]: value }));
  }

  function validate(): boolean {
    const errs: Partial<SaleForm> = {};
    if (!form.amount || parseFloat(form.amount) <= 0)
      errs.amount = "Ingresá un monto válido mayor a 0.";
    if (!form.quantity || parseInt(form.quantity) < 1)
      errs.quantity = "Ingresá al menos 1.";
    if (!form.transaction_date) errs.transaction_date = "Requerido.";
    setErrors(errs);
    const cErrs = validateRequiredCustom(customDefs, customValues);
    setCustomErrors(cErrs);
    return Object.keys(errs).length === 0 && Object.keys(cErrs).length === 0;
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!validate()) return;
    const custom = cleanCustom(customValues);
    setSubmitting(true);
    await submit(
      "sale",
      {
        amount: parseFloat(form.amount),
        quantity: parseInt(form.quantity),
        transaction_date: form.transaction_date,
        payment_method: form.payment_method,
        product_id: form.product_id || null,
        customer_id: form.customer_id || null,
        notes: form.notes || null,
        ...(Object.keys(custom).length ? { custom_fields: custom } : {}),
      },
      {
        onSuccess: () => {
          onToast({ type: "success", message: "Venta registrada correctamente." });
          resetForm();
        },
        onQueued: () => {
          onToast({
            type: "success",
            message: "Guardado sin conexión, se sincronizará al volver online.",
          });
          resetForm();
        },
        onError: () => {
          onToast({ type: "error", message: "No se pudo registrar la venta. Revisá los datos." });
        },
      },
    );
    setSubmitting(false);
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <div className="grid grid-cols-2 gap-4">
        <Input
          label="Monto ($)"
          type="number"
          min={0}
          step="any"
          placeholder="0.00"
          value={form.amount}
          onChange={set("amount")}
          error={errors.amount}
        />
        <Input
          label="Cantidad"
          type="number"
          min={1}
          step={1}
          placeholder="1"
          value={form.quantity}
          onChange={set("quantity")}
          error={errors.quantity}
        />
      </div>

      <div className="grid grid-cols-2 gap-4">
        <Input
          label="Fecha y hora"
          type="datetime-local"
          max={nowStr()}
          value={form.transaction_date}
          onChange={set("transaction_date")}
          error={errors.transaction_date}
        />
        <div className="flex flex-col gap-1.5">
          <label className="text-xs font-medium text-vk-text-secondary">
            Método de pago
          </label>
          <select
            value={form.payment_method}
            onChange={set("payment_method")}
            className={selectClass}
          >
            {PAYMENT_METHODS.map((m) => (
              <option key={m.value} value={m.value}>
                {m.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="flex flex-col gap-1.5">
        <label className="text-xs font-medium text-vk-text-secondary">
          Producto (opcional)
        </label>
        <select
          value={form.product_id}
          onChange={set("product_id")}
          className={selectClass}
        >
          <option value="">Sin producto asociado</option>
          {(products ?? []).map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      </div>

      <div className="flex flex-col gap-1.5">
        <label className="text-xs font-medium text-vk-text-secondary">
          Cliente (opcional)
        </label>
        <select
          value={form.customer_id}
          onChange={set("customer_id")}
          className={selectClass}
        >
          <option value="">Sin cliente</option>
          {(customers ?? []).map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </select>
      </div>

      <Input
        label="Notas (opcional)"
        type="text"
        placeholder="Ej: venta mayorista, descuento aplicado..."
        value={form.notes}
        onChange={set("notes")}
      />

      <CustomFieldsForm
        entityType="sale"
        values={customValues}
        onChange={setCustom}
        errors={customErrors}
      />

      <Button type="submit" size="sm" loading={submitting}>
        Registrar venta
      </Button>
    </form>
  );
}

// ── Expense form ──────────────────────────────────────────────────────────────

const EXPENSE_CATEGORIES = ALL_CATEGORIES.map((value) => ({
  value,
  label: CATEGORY_LABELS[value],
}));

interface ExpenseForm {
  amount: string;
  category: string;
  category_label: string;
  expense_type: string;
  payment_method: string;
  supplier_name: string;
  supplier_id: string;
  expense_date: string;
  description: string;
  is_recurring: boolean;
}

function emptyExpenseForm(): ExpenseForm {
  return {
    amount: "",
    category: "OTHER",
    category_label: "",
    expense_type: "OPEX",
    payment_method: "transfer",
    supplier_name: "",
    supplier_id: "",
    expense_date: nowStr(),
    description: "",
    is_recurring: false,
  };
}

function ExpenseTab({ onToast }: { onToast: (t: ToastState) => void }) {
  const { submit } = useOfflineSubmit();
  const [submitting, setSubmitting] = useState(false);
  const [form, setForm] = useState<ExpenseForm>(emptyExpenseForm);
  const [errors, setErrors] = useState<Partial<Record<keyof ExpenseForm, string>>>({});
  const [customValues, setCustomValues] = useState<Record<string, unknown>>({});
  const [customErrors, setCustomErrors] = useState<Record<string, string>>({});

  const { data: customDefs } = useQuery({
    queryKey: ["field-definitions", "expense"],
    queryFn: () => fieldDefinitionsService.getAll("expense"),
  });
  const { data: suppliers } = useQuery({
    queryKey: ["suppliers-list"],
    queryFn: () => suppliersService.getAllSuppliers(),
  });

  function resetForm() {
    setForm(emptyExpenseForm());
    setErrors({});
    setCustomValues({});
    setCustomErrors({});
  }

  function set(key: keyof ExpenseForm) {
    return (
      e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>,
    ) =>
      setForm((prev) => ({
        ...prev,
        [key]:
          key === "is_recurring"
            ? (e.target as HTMLInputElement).checked
            : e.target.value,
      }));
  }

  function setCustom(fieldKey: string, value: unknown) {
    setCustomValues((prev) => ({ ...prev, [fieldKey]: value }));
  }

  function validate(): boolean {
    const errs: Partial<Record<keyof ExpenseForm, string>> = {};
    if (!form.amount || parseFloat(form.amount) <= 0)
      errs.amount = "Ingresá un monto válido mayor a 0.";
    if (!form.expense_date) errs.expense_date = "Requerido.";
    setErrors(errs);
    const cErrs = validateRequiredCustom(customDefs, customValues);
    setCustomErrors(cErrs);
    return Object.keys(errs).length === 0 && Object.keys(cErrs).length === 0;
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!validate()) return;
    const custom = cleanCustom(customValues);
    setSubmitting(true);
    await submit(
      "expense",
      {
        amount: parseFloat(form.amount),
        category: form.category,
        // Solo se envía el nombre personalizado cuando la categoría es "Otro".
        ...(form.category === "OTHER" && form.category_label.trim()
          ? { category_label: form.category_label.trim() }
          : {}),
        expense_type: form.expense_type === "COGS" ? "COGS" : "OPEX",
        payment_method: form.payment_method,
        supplier_name: form.supplier_name.trim() || null,
        supplier_id: form.supplier_id || null,
        expense_date: form.expense_date,
        description: form.description || "",
        is_recurring: form.is_recurring,
        ...(Object.keys(custom).length ? { custom_fields: custom } : {}),
      },
      {
        onSuccess: () => {
          onToast({ type: "success", message: "Gasto registrado correctamente." });
          resetForm();
        },
        onQueued: () => {
          onToast({
            type: "success",
            message: "Guardado sin conexión, se sincronizará al volver online.",
          });
          resetForm();
        },
        onError: () => {
          onToast({ type: "error", message: "No se pudo registrar el gasto. Revisá los datos." });
        },
      },
    );
    setSubmitting(false);
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <div className="grid grid-cols-2 gap-4">
        <Input
          label="Monto ($)"
          type="number"
          min={0}
          step="any"
          placeholder="0.00"
          value={form.amount}
          onChange={set("amount")}
          error={errors.amount}
        />
        <div className="flex flex-col gap-1.5">
          <label className="text-xs font-medium text-vk-text-secondary">Categoría</label>
          <select
            value={form.category}
            onChange={set("category")}
            className={selectClass}
          >
            {EXPENSE_CATEGORIES.map((c) => (
              <option key={c.value} value={c.value}>
                {c.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      {form.category === "OTHER" && (
        <Input
          label="Nombre de la categoría (opcional)"
          placeholder="Ej: Limpieza, Veterinaria, Combustible…"
          maxLength={50}
          value={form.category_label}
          onChange={set("category_label")}
        />
      )}

      <div className="grid grid-cols-2 gap-4">
        <div className="flex flex-col gap-1.5">
          <label className="text-xs font-medium text-vk-text-secondary">
            Tipo contable
          </label>
          <select
            value={form.expense_type}
            onChange={set("expense_type")}
            className={selectClass}
          >
            {EXPENSE_TYPES.map((t) => (
              <option key={t.value} value={t.value}>
                {t.label}
              </option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-1.5">
          <label className="text-xs font-medium text-vk-text-secondary">
            Método de pago
          </label>
          <select
            value={form.payment_method}
            onChange={set("payment_method")}
            className={selectClass}
          >
            {PAYMENT_METHODS.map((m) => (
              <option key={m.value} value={m.value}>
                {m.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-4">
        <Input
          label="Fecha y hora"
          type="datetime-local"
          max={nowStr()}
          value={form.expense_date}
          onChange={set("expense_date")}
          error={errors.expense_date}
        />
        <Input
          label="Proveedor — texto libre (opcional)"
          type="text"
          placeholder="Ej: Distribuidora del Sur"
          maxLength={300}
          value={form.supplier_name}
          onChange={set("supplier_name")}
        />
      </div>

      <div className="flex flex-col gap-1.5">
        <label className="text-xs font-medium text-vk-text-secondary">
          Proveedor (opcional)
        </label>
        <select
          value={form.supplier_id}
          onChange={set("supplier_id")}
          className={selectClass}
        >
          <option value="">Sin proveedor</option>
          {(suppliers ?? []).map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </select>
      </div>

      <Input
        label="Descripción (opcional)"
        type="text"
        placeholder="Ej: pago mensual alquiler"
        value={form.description}
        onChange={set("description")}
      />

      <label className="flex cursor-pointer items-center gap-2.5">
        <input
          type="checkbox"
          checked={form.is_recurring}
          onChange={set("is_recurring")}
          className="h-4 w-4 rounded border-vk-border-w accent-vk-blue"
        />
        <span className="text-sm text-vk-text-secondary">Gasto recurrente</span>
      </label>

      <CustomFieldsForm
        entityType="expense"
        values={customValues}
        onChange={setCustom}
        errors={customErrors}
      />

      <Button type="submit" size="sm" loading={submitting}>
        Registrar gasto
      </Button>
    </form>
  );
}

// ── Product form ──────────────────────────────────────────────────────────────

interface ProductForm {
  name: string;
  sale_price_ars: string;
  unit_cost_ars: string;
  stock_units: string;
  low_stock_threshold_units: string;
  category: string;
  sku: string;
  description: string;
  acquired_at: string;
}

function emptyProductForm(): ProductForm {
  return {
    name: "",
    sale_price_ars: "",
    unit_cost_ars: "",
    stock_units: "0",
    low_stock_threshold_units: "",
    category: "",
    sku: "",
    description: "",
    acquired_at: nowStr(),
  };
}

function ProductTab({ onToast }: { onToast: (t: ToastState) => void }) {
  const { submit } = useOfflineSubmit();
  const [submitting, setSubmitting] = useState(false);
  const [form, setForm] = useState<ProductForm>(emptyProductForm);
  const [errors, setErrors] = useState<Partial<Record<keyof ProductForm, string>>>({});
  const [customValues, setCustomValues] = useState<Record<string, unknown>>({});
  const [customErrors, setCustomErrors] = useState<Record<string, string>>({});

  const { data: customDefs } = useQuery({
    queryKey: ["field-definitions", "product"],
    queryFn: () => fieldDefinitionsService.getAll("product"),
  });

  function resetForm() {
    setForm(emptyProductForm());
    setErrors({});
    setCustomValues({});
    setCustomErrors({});
  }

  function set(key: keyof ProductForm) {
    return (e: React.ChangeEvent<HTMLInputElement>) =>
      setForm((prev) => ({ ...prev, [key]: e.target.value }));
  }

  function setCustom(fieldKey: string, value: unknown) {
    setCustomValues((prev) => ({ ...prev, [fieldKey]: value }));
  }

  function validate(): boolean {
    const errs: Partial<Record<keyof ProductForm, string>> = {};
    if (!form.name.trim()) errs.name = "El nombre es requerido.";
    if (!form.sale_price_ars || parseFloat(form.sale_price_ars) <= 0)
      errs.sale_price_ars = "Ingresá un precio de venta válido.";
    if (form.unit_cost_ars && parseFloat(form.unit_cost_ars) <= 0)
      errs.unit_cost_ars = "El costo debe ser mayor a 0.";
    setErrors(errs);
    const cErrs = validateRequiredCustom(customDefs, customValues);
    setCustomErrors(cErrs);
    return Object.keys(errs).length === 0 && Object.keys(cErrs).length === 0;
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!validate()) return;
    const custom = cleanCustom(customValues);
    setSubmitting(true);
    await submit(
      "product",
      {
        name: form.name.trim(),
        sale_price_ars: parseFloat(form.sale_price_ars),
        unit_cost_ars: form.unit_cost_ars ? parseFloat(form.unit_cost_ars) : null,
        stock_units: form.stock_units ? parseInt(form.stock_units) : 0,
        // nullable: vacío → omitir (servidor aplica default); "0" → 0 explícito.
        ...(form.low_stock_threshold_units !== ""
          ? { low_stock_threshold_units: parseInt(form.low_stock_threshold_units) }
          : {}),
        category: form.category.trim() || null,
        sku: form.sku.trim() || null,
        description: form.description.trim() || null,
        acquired_at: form.acquired_at || null,
        ...(Object.keys(custom).length ? { custom_fields: custom } : {}),
      },
      {
        onSuccess: () => {
          onToast({ type: "success", message: "Producto agregado correctamente." });
          resetForm();
        },
        onQueued: () => {
          onToast({
            type: "success",
            message: "Guardado sin conexión, se sincronizará al volver online.",
          });
          resetForm();
        },
        onError: () => {
          onToast({ type: "error", message: "No se pudo agregar el producto. Revisá los datos." });
        },
      },
    );
    setSubmitting(false);
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <Input
        label="Nombre del producto"
        type="text"
        placeholder="Ej: Agua mineral 500ml"
        value={form.name}
        onChange={set("name")}
        error={errors.name}
      />

      <div className="grid grid-cols-2 gap-4">
        <Input
          label="Precio de venta ($)"
          type="number"
          min={0}
          step="any"
          placeholder="0.00"
          value={form.sale_price_ars}
          onChange={set("sale_price_ars")}
          error={errors.sale_price_ars}
        />
        <Input
          label="Costo unitario ($, opcional)"
          type="number"
          min={0}
          step="any"
          placeholder="0.00"
          value={form.unit_cost_ars}
          onChange={set("unit_cost_ars")}
          error={errors.unit_cost_ars}
        />
      </div>

      <div className="grid grid-cols-2 gap-4">
        <Input
          label="Stock inicial"
          type="number"
          min={0}
          step={1}
          placeholder="0"
          value={form.stock_units}
          onChange={set("stock_units")}
        />
        <Input
          label="Umbral de stock bajo (opcional)"
          type="number"
          min={0}
          step={1}
          placeholder="Sin configurar"
          hint="Vacío = default del sistema. 0 = solo alerta al quedar sin stock."
          value={form.low_stock_threshold_units}
          onChange={set("low_stock_threshold_units")}
        />
      </div>

      <div className="grid grid-cols-2 gap-4">
        <Input
          label="SKU (opcional)"
          type="text"
          placeholder="Ej: AGUA-500"
          maxLength={100}
          value={form.sku}
          onChange={set("sku")}
        />
        <Input
          label="Categoría (opcional)"
          type="text"
          placeholder="Ej: Bebidas, Limpieza..."
          value={form.category}
          onChange={set("category")}
        />
      </div>

      <div className="grid grid-cols-2 gap-4">
        <Input
          label="Descripción (opcional)"
          type="text"
          placeholder="Ej: pack x6"
          value={form.description}
          onChange={set("description")}
        />
        <Input
          label="Fecha de alta"
          type="datetime-local"
          max={nowStr()}
          value={form.acquired_at}
          onChange={set("acquired_at")}
        />
      </div>

      <CustomFieldsForm
        entityType="product"
        values={customValues}
        onChange={setCustom}
        errors={customErrors}
      />

      <Button type="submit" size="sm" loading={submitting} aria-label="Registrar compra">
        Registrar compra
      </Button>
    </form>
  );
}

// ── ManualEntrySection ────────────────────────────────────────────────────────

type ActiveTab = "sale" | "expense" | "product";

const TABS: { key: ActiveTab; label: string }[] = [
  { key: "sale", label: "Registrar venta" },
  { key: "expense", label: "Registrar gasto" },
  { key: "product", label: "Registrar compra" },
];

/**
 * Contenido de carga manual (tabs + formularios completos + custom fields por vertical).
 * Se renderiza dentro del Modal flotante que abre `ManualEntryLauncher`.
 */
export function ManualEntrySection() {
  const [activeTab, setActiveTab] = useState<ActiveTab>("sale");
  const [toast, setToast] = useState<ToastState>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pending = useOfflineQueueCount();
  const online = useOnlineStatus();

  function handleToast(t: ToastState) {
    if (toastTimer.current) clearTimeout(toastTimer.current);
    setToast(t);
    toastTimer.current = setTimeout(() => setToast(null), 3_000);
  }

  useEffect(() => {
    return () => {
      if (toastTimer.current) clearTimeout(toastTimer.current);
    };
  }, []);

  return (
    <div>
      {/* Estado de conexión / cola offline */}
      {(!online || pending > 0) && (
        <div
          className={[
            "mb-4 rounded-lg border px-3 py-2 text-xs",
            online
              ? "border-vk-blue/20 bg-vk-blue/5 text-vk-text-secondary"
              : "border-vk-warning/30 bg-vk-warning-bg text-vk-warning",
          ].join(" ")}
        >
          {!online && "Sin conexión — las cargas se guardan y se sincronizan al volver online. "}
          {pending > 0 && `${pending} carga(s) pendientes de sincronizar.`}
        </div>
      )}

      {/* Tabs */}
      <div className="mb-6 flex gap-1 rounded-lg border border-vk-border-w bg-vk-bg-light p-1">
        {TABS.map((tab) => (
          <button
            key={tab.key}
            type="button"
            onClick={() => setActiveTab(tab.key)}
            className={[
              "flex-1 rounded-md px-3 py-1.5 text-xs font-medium transition-colors",
              activeTab === tab.key
                ? "bg-vk-surface-w text-vk-text-primary shadow-vk-sm"
                : "text-vk-text-muted hover:text-vk-text-secondary",
            ].join(" ")}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Toast */}
      {toast && (
        <div className="mb-4">
          <Toast toast={toast} />
        </div>
      )}

      {/* Active form */}
      {activeTab === "sale" && <SaleTab onToast={handleToast} />}
      {activeTab === "expense" && <ExpenseTab onToast={handleToast} />}
      {activeTab === "product" && <ProductTab onToast={handleToast} />}
    </div>
  );
}
