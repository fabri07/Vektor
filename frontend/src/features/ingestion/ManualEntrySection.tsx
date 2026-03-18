"use client";

import { useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { salesService } from "@/services/sales.service";
import { expensesService } from "@/services/expenses.service";
import { productsService } from "@/services/products.service";

// ── helpers ──────────────────────────────────────────────────────────────────

function todayStr(): string {
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
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

// ── Sale form ─────────────────────────────────────────────────────────────────

const PAYMENT_METHODS = [
  { value: "cash", label: "Efectivo" },
  { value: "debit_card", label: "Tarjeta débito" },
  { value: "credit_card", label: "Tarjeta crédito" },
  { value: "transfer", label: "Transferencia" },
  { value: "qr", label: "QR / Mercado Pago" },
  { value: "other", label: "Otro" },
];

interface SaleForm {
  amount: string;
  quantity: string;
  transaction_date: string;
  payment_method: string;
  notes: string;
}

function emptySaleForm(): SaleForm {
  return {
    amount: "",
    quantity: "1",
    transaction_date: todayStr(),
    payment_method: "cash",
    notes: "",
  };
}

function SaleTab({ onToast }: { onToast: (t: ToastState) => void }) {
  const queryClient = useQueryClient();
  const [form, setForm] = useState<SaleForm>(emptySaleForm);
  const [errors, setErrors] = useState<Partial<SaleForm>>({});

  const mutation = useMutation({
    mutationFn: salesService.createSale,
    onSuccess: () => {
      onToast({ type: "success", message: "Venta registrada correctamente." });
      setForm(emptySaleForm());
      setErrors({});
      void queryClient.invalidateQueries({ queryKey: ["ingestion-files"] });
    },
    onError: () => {
      onToast({ type: "error", message: "No se pudo registrar la venta. Revisá los datos." });
    },
  });

  function set(key: keyof SaleForm) {
    return (
      e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>,
    ) => setForm((prev) => ({ ...prev, [key]: e.target.value }));
  }

  function validate(): boolean {
    const errs: Partial<SaleForm> = {};
    if (!form.amount || parseFloat(form.amount) <= 0)
      errs.amount = "Ingresá un monto válido mayor a 0.";
    if (!form.quantity || parseInt(form.quantity) < 1)
      errs.quantity = "Ingresá al menos 1.";
    if (!form.transaction_date) errs.transaction_date = "Requerido.";
    setErrors(errs);
    return Object.keys(errs).length === 0;
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!validate()) return;
    mutation.mutate({
      amount: parseFloat(form.amount),
      quantity: parseInt(form.quantity),
      transaction_date: form.transaction_date,
      payment_method: form.payment_method,
      notes: form.notes || null,
    });
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
          label="Fecha"
          type="date"
          max={todayStr()}
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

      <Input
        label="Notas (opcional)"
        type="text"
        placeholder="Ej: venta mayorista, descuento aplicado..."
        value={form.notes}
        onChange={set("notes")}
      />

      <Button type="submit" size="sm" loading={mutation.isPending}>
        Registrar venta
      </Button>
    </form>
  );
}

// ── Expense form ──────────────────────────────────────────────────────────────

const EXPENSE_CATEGORIES = [
  { value: "RENT", label: "Alquiler" },
  { value: "UTILITIES", label: "Servicios (luz, gas, internet)" },
  { value: "PAYROLL", label: "Sueldos y personal" },
  { value: "INVENTORY", label: "Mercadería / Stock" },
  { value: "MARKETING", label: "Marketing y publicidad" },
  { value: "OTHER", label: "Otro" },
];

interface ExpenseForm {
  amount: string;
  category: string;
  expense_date: string;
  description: string;
  is_recurring: boolean;
}

function emptyExpenseForm(): ExpenseForm {
  return {
    amount: "",
    category: "OTHER",
    expense_date: todayStr(),
    description: "",
    is_recurring: false,
  };
}

function ExpenseTab({ onToast }: { onToast: (t: ToastState) => void }) {
  const queryClient = useQueryClient();
  const [form, setForm] = useState<ExpenseForm>(emptyExpenseForm);
  const [errors, setErrors] = useState<Partial<Record<keyof ExpenseForm, string>>>({});

  const mutation = useMutation({
    mutationFn: expensesService.createExpense,
    onSuccess: () => {
      onToast({ type: "success", message: "Gasto registrado correctamente." });
      setForm(emptyExpenseForm());
      setErrors({});
      void queryClient.invalidateQueries({ queryKey: ["ingestion-files"] });
    },
    onError: () => {
      onToast({ type: "error", message: "No se pudo registrar el gasto. Revisá los datos." });
    },
  });

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

  function validate(): boolean {
    const errs: Partial<Record<keyof ExpenseForm, string>> = {};
    if (!form.amount || parseFloat(form.amount) <= 0)
      errs.amount = "Ingresá un monto válido mayor a 0.";
    if (!form.expense_date) errs.expense_date = "Requerido.";
    setErrors(errs);
    return Object.keys(errs).length === 0;
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!validate()) return;
    mutation.mutate({
      amount: parseFloat(form.amount),
      category: form.category,
      expense_date: form.expense_date,
      description: form.description || "",
      is_recurring: form.is_recurring,
    });
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

      <div className="grid grid-cols-2 gap-4">
        <Input
          label="Fecha"
          type="date"
          max={todayStr()}
          value={form.expense_date}
          onChange={set("expense_date")}
          error={errors.expense_date}
        />
        <Input
          label="Descripción (opcional)"
          type="text"
          placeholder="Ej: pago mensual alquiler"
          value={form.description}
          onChange={set("description")}
        />
      </div>

      <label className="flex cursor-pointer items-center gap-2.5">
        <input
          type="checkbox"
          checked={form.is_recurring}
          onChange={set("is_recurring")}
          className="h-4 w-4 rounded border-vk-border-w accent-vk-blue"
        />
        <span className="text-sm text-vk-text-secondary">Gasto recurrente</span>
      </label>

      <Button type="submit" size="sm" loading={mutation.isPending}>
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
  category: string;
}

function emptyProductForm(): ProductForm {
  return {
    name: "",
    sale_price_ars: "",
    unit_cost_ars: "",
    stock_units: "0",
    category: "",
  };
}

function ProductTab({ onToast }: { onToast: (t: ToastState) => void }) {
  const queryClient = useQueryClient();
  const [form, setForm] = useState<ProductForm>(emptyProductForm);
  const [errors, setErrors] = useState<Partial<Record<keyof ProductForm, string>>>({});

  const mutation = useMutation({
    mutationFn: productsService.createProduct,
    onSuccess: () => {
      onToast({ type: "success", message: "Producto agregado correctamente." });
      setForm(emptyProductForm());
      setErrors({});
      void queryClient.invalidateQueries({ queryKey: ["ingestion-files"] });
    },
    onError: () => {
      onToast({ type: "error", message: "No se pudo agregar el producto. Revisá los datos." });
    },
  });

  function set(key: keyof ProductForm) {
    return (e: React.ChangeEvent<HTMLInputElement>) =>
      setForm((prev) => ({ ...prev, [key]: e.target.value }));
  }

  function validate(): boolean {
    const errs: Partial<Record<keyof ProductForm, string>> = {};
    if (!form.name.trim()) errs.name = "El nombre es requerido.";
    if (!form.sale_price_ars || parseFloat(form.sale_price_ars) <= 0)
      errs.sale_price_ars = "Ingresá un precio de venta válido.";
    if (form.unit_cost_ars && parseFloat(form.unit_cost_ars) <= 0)
      errs.unit_cost_ars = "El costo debe ser mayor a 0.";
    setErrors(errs);
    return Object.keys(errs).length === 0;
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!validate()) return;
    mutation.mutate({
      name: form.name.trim(),
      sale_price_ars: parseFloat(form.sale_price_ars),
      unit_cost_ars: form.unit_cost_ars ? parseFloat(form.unit_cost_ars) : null,
      stock_units: form.stock_units ? parseInt(form.stock_units) : 0,
      category: form.category.trim() || null,
    });
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
          label="Categoría (opcional)"
          type="text"
          placeholder="Ej: Bebidas, Limpieza..."
          value={form.category}
          onChange={set("category")}
        />
      </div>

      <Button type="submit" size="sm" loading={mutation.isPending}>
        Agregar producto
      </Button>
    </form>
  );
}

// ── ManualEntrySection ────────────────────────────────────────────────────────

type ActiveTab = "sale" | "expense" | "product";

const TABS: { key: ActiveTab; label: string }[] = [
  { key: "sale", label: "Registrar venta" },
  { key: "expense", label: "Registrar gasto" },
  { key: "product", label: "Agregar producto" },
];

export function ManualEntrySection() {
  const [activeTab, setActiveTab] = useState<ActiveTab>("sale");
  const [toast, setToast] = useState<ToastState>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

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
    <div className="rounded-xl border border-vk-border-w bg-vk-surface-w p-6 shadow-vk-sm">
      <h2 className="mb-4 text-sm font-semibold text-vk-text-primary">Carga manual</h2>

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
