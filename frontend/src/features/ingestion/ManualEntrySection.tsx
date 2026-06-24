"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { productsService, type ProductResponse } from "@/services/products.service";
import { customersService } from "@/services/customers.service";
import { suppliersService } from "@/services/suppliers.service";
import { expensesService } from "@/services/expenses.service";
import { ALL_CATEGORIES, CATEGORY_LABELS } from "@/lib/expenseCategories";
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

function cleanCustom(values: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(values).filter(([, v]) => v !== undefined && v !== null && v !== ""),
  );
}

function fmtArs(n: number): string {
  return new Intl.NumberFormat("es-AR", {
    style: "currency",
    currency: "ARS",
    maximumFractionDigits: 0,
  }).format(Number.isFinite(n) ? n : 0);
}

// Redondeo a 2 decimales: el backend exige decimal_places=2 (montos en ARS).
function round2(n: number): number {
  return Math.round((Number.isFinite(n) ? n : 0) * 100) / 100;
}

function marginPct(cost: number, price: number): number | null {
  if (!price || price <= 0) return null;
  return Math.round(((price - cost) / price) * 1000) / 10;
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

// ── estilos compartidos ─────────────────────────────────────────────────────

const selectClass =
  "h-9 w-full rounded-lg border border-vk-border-w bg-vk-surface-w px-3 text-sm text-vk-text-primary focus:outline-none focus:ring-2 focus:ring-vk-blue/15 focus:border-vk-blue/40 disabled:opacity-40";
const labelClass = "text-xs font-medium text-vk-text-secondary";

const PAYMENT_METHODS = [
  { value: "cash", label: "Efectivo" },
  { value: "debit_card", label: "Tarjeta débito" },
  { value: "credit_card", label: "Tarjeta crédito" },
  { value: "transfer", label: "Transferencia" },
  { value: "qr", label: "QR / Mercado Pago" },
  { value: "account", label: "Cuenta corriente (fiado)" },
  { value: "other", label: "Otro" },
];

const NEW_OPTION = "__new__";

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <label className={labelClass}>{label}</label>
      {children}
    </div>
  );
}

function FieldError({ msg }: { msg?: string }) {
  return msg ? <p className="text-xs text-vk-danger">{msg}</p> : null;
}

// ── data hooks (react-query) ────────────────────────────────────────────────

function useProducts() {
  return useQuery({
    queryKey: ["products-list"],
    queryFn: () => productsService.getAllProducts({ is_active: true }),
  });
}
function useCustomers() {
  return useQuery({
    queryKey: ["customers-list"],
    queryFn: () => customersService.getAllCustomers(),
  });
}
function useSuppliers() {
  return useQuery({
    queryKey: ["suppliers-list"],
    queryFn: () => suppliersService.getAllSuppliers(),
  });
}
function useProductCategories() {
  return useQuery({
    queryKey: ["product-categories"],
    queryFn: () => productsService.getCategories(),
  });
}

// Categoría a la que pertenece un producto (code de catálogo). null → "Sin categoría".
function productCategoryCode(p: ProductResponse): string {
  return p.category ?? "__none__";
}

// ════════════════════════════ VENTA ════════════════════════════

interface SaleLine {
  id: string;
  category: string;
  product_id: string;
  quantity: string;
  unit_price: string;
}

function newSaleLine(): SaleLine {
  return { id: crypto.randomUUID(), category: "", product_id: "", quantity: "1", unit_price: "" };
}

function SaleTab({ onToast, online }: { onToast: (t: ToastState) => void; online: boolean }) {
  const { submit } = useOfflineSubmit();
  const queryClient = useQueryClient();
  const { data: products } = useProducts();
  const { data: customers } = useCustomers();
  const { data: categories } = useProductCategories();

  const [paymentMethod, setPaymentMethod] = useState("cash");
  const [transactionDate, setTransactionDate] = useState(nowStr);
  const [customerId, setCustomerId] = useState(""); // "" = Local (por defecto)
  const [notes, setNotes] = useState("");
  const [lines, setLines] = useState<SaleLine[]>([newSaleLine()]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showNewCustomer, setShowNewCustomer] = useState(false);

  const isFiado = paymentMethod === "account";

  const productsByCat = useMemo(() => {
    const map = new Map<string, ProductResponse[]>();
    for (const p of products ?? []) {
      const k = productCategoryCode(p);
      if (!map.has(k)) map.set(k, []);
      map.get(k)!.push(p);
    }
    return map;
  }, [products]);

  function setLine(id: string, patch: Partial<SaleLine>) {
    setLines((prev) => prev.map((l) => (l.id === id ? { ...l, ...patch } : l)));
  }
  function onPickProduct(id: string, productId: string) {
    const p = (products ?? []).find((x) => x.id === productId);
    setLine(id, {
      product_id: productId,
      unit_price: p ? String(p.sale_price_ars) : "",
    });
  }

  const total = lines.reduce((s, l) => {
    const q = parseInt(l.quantity) || 0;
    const u = parseFloat(l.unit_price) || 0;
    return s + q * u;
  }, 0);

  function reset() {
    setPaymentMethod("cash");
    setTransactionDate(nowStr());
    setCustomerId("");
    setNotes("");
    setLines([newSaleLine()]);
    setError(null);
    setShowNewCustomer(false);
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    if (isFiado && !customerId) {
      setError("El fiado (cuenta corriente) requiere un cliente registrado (no 'Local').");
      return;
    }
    const items = lines
      .filter((l) => l.product_id)
      .map((l) => ({
        product_id: l.product_id,
        quantity: parseInt(l.quantity) || 0,
        unit_price: round2(parseFloat(l.unit_price) || 0),
      }));
    if (items.length === 0) {
      setError("Agregá al menos un producto a la venta.");
      return;
    }
    if (items.some((i) => i.quantity < 1 || i.unit_price <= 0)) {
      setError("Revisá cantidades (≥1) y precios (>0) de cada producto.");
      return;
    }
    setSubmitting(true);
    await submit(
      "sale_batch",
      {
        customer_id: customerId || null,
        payment_method: paymentMethod,
        transaction_date: transactionDate,
        notes: notes.trim() || null,
        items,
      },
      {
        onSuccess: () => {
          onToast({ type: "success", message: "Venta registrada y stock actualizado." });
          reset();
        },
        onQueued: () => {
          onToast({
            type: "success",
            message: "Guardado sin conexión, se sincronizará al volver online.",
          });
          reset();
        },
        onError: () => {
          onToast({
            type: "error",
            message: "No se pudo registrar la venta. Revisá stock y datos.",
          });
        },
      },
    );
    setSubmitting(false);
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      {/* Cabecera */}
      <div className="grid gap-4 sm:grid-cols-3">
        <Field label="Cliente">
          <select
            value={showNewCustomer ? NEW_OPTION : customerId}
            onChange={(e) => {
              if (e.target.value === NEW_OPTION) {
                setShowNewCustomer(true);
              } else {
                setShowNewCustomer(false);
                setCustomerId(e.target.value);
              }
            }}
            className={selectClass}
          >
            <option value="">Local (por defecto)</option>
            {(customers ?? []).map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
                {c.last_name ? ` ${c.last_name}` : ""}
              </option>
            ))}
            {online && <option value={NEW_OPTION}>+ Registrar cliente nuevo</option>}
          </select>
          {!online && (
            <p className="text-xs text-vk-text-muted">
              Crear clientes nuevos requiere conexión.
            </p>
          )}
        </Field>
        <Field label="Fecha y hora">
          <input
            type="datetime-local"
            max={nowStr()}
            value={transactionDate}
            onChange={(e) => setTransactionDate(e.target.value)}
            className={selectClass}
          />
        </Field>
        <Field label="Método de pago">
          <select
            value={paymentMethod}
            onChange={(e) => setPaymentMethod(e.target.value)}
            className={selectClass}
          >
            {PAYMENT_METHODS.map((m) => (
              <option key={m.value} value={m.value}>
                {m.label}
              </option>
            ))}
          </select>
        </Field>
      </div>

      {showNewCustomer && (
        <InlineCustomerCreate
          onCreated={async (id) => {
            setShowNewCustomer(false);
            setCustomerId(id);
            await queryClient.invalidateQueries({ queryKey: ["customers-list"] });
          }}
          onCancel={() => setShowNewCustomer(false)}
        />
      )}

      {/* Líneas de productos */}
      <div className="space-y-2">
        <p className={labelClass}>Productos</p>
        {lines.map((l) => {
          const opts = l.category
            ? (productsByCat.get(l.category) ?? [])
            : (products ?? []);
          const subtotal = (parseInt(l.quantity) || 0) * (parseFloat(l.unit_price) || 0);
          return (
            <div
              key={l.id}
              className="grid grid-cols-1 gap-2 rounded-lg border border-vk-border-w p-2 sm:grid-cols-[1fr_1.4fr_0.6fr_0.8fr_auto]"
            >
              <select
                value={l.category}
                onChange={(e) => setLine(l.id, { category: e.target.value, product_id: "" })}
                className={selectClass}
              >
                <option value="">Todas las categorías</option>
                {(categories ?? []).map((c) => (
                  <option key={c.code} value={c.code}>
                    {c.label}
                  </option>
                ))}
                {(products ?? []).some((p) => !p.category) && (
                  <option value="__none__">Sin categoría</option>
                )}
              </select>
              <select
                value={l.product_id}
                onChange={(e) => onPickProduct(l.id, e.target.value)}
                className={selectClass}
              >
                <option value="">Producto…</option>
                {opts.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name} (stock {p.stock_units})
                  </option>
                ))}
              </select>
              <input
                type="number"
                min={1}
                step={1}
                value={l.quantity}
                onChange={(e) => setLine(l.id, { quantity: e.target.value })}
                placeholder="Cant."
                className={selectClass}
              />
              <input
                type="number"
                min={0}
                step="any"
                value={l.unit_price}
                onChange={(e) => setLine(l.id, { unit_price: e.target.value })}
                placeholder="P. unit."
                className={selectClass}
              />
              <div className="flex items-center justify-between gap-2 sm:justify-end">
                <span className="text-sm font-medium tabular-nums text-vk-text-primary">
                  {fmtArs(subtotal)}
                </span>
                <button
                  type="button"
                  aria-label="Eliminar línea"
                  disabled={lines.length === 1}
                  onClick={() => setLines((prev) => prev.filter((x) => x.id !== l.id))}
                  className="inline-flex h-8 w-8 items-center justify-center rounded border border-vk-danger/30 text-vk-danger transition-colors hover:bg-vk-danger-bg disabled:opacity-40"
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>
            </div>
          );
        })}
        <button
          type="button"
          onClick={() => setLines((prev) => [...prev, newSaleLine()])}
          className="inline-flex items-center gap-1.5 text-sm font-medium text-vk-blue hover:underline"
        >
          <Plus className="h-4 w-4" /> Agregar producto
        </button>
      </div>

      <Input
        label="Notas (opcional)"
        type="text"
        placeholder="Ej: venta mayorista, descuento aplicado…"
        value={notes}
        onChange={(e) => setNotes(e.target.value)}
      />

      <FieldError msg={error ?? undefined} />

      {/* Pie: total + submit */}
      <div className="flex items-center justify-between border-t border-vk-border-w pt-3">
        <span className="text-sm text-vk-text-secondary">
          Total: <span className="font-semibold text-vk-text-primary">{fmtArs(total)}</span>
        </span>
        <Button type="submit" size="sm" loading={submitting}>
          Registrar venta
        </Button>
      </div>
    </form>
  );
}

// ════════════════════════════ GASTO ════════════════════════════

// Categorías canónicas SIN mercadería (INVENTORY): la compra de mercadería va en su tab.
const OPEX_CATEGORIES = ALL_CATEGORIES.filter((c) => c !== "INVENTORY").map((value) => ({
  value,
  label: CATEGORY_LABELS[value],
}));

function ExpenseTab({ onToast, online }: { onToast: (t: ToastState) => void; online: boolean }) {
  const { submit } = useOfflineSubmit();
  const queryClient = useQueryClient();
  const { data: suppliers } = useSuppliers();
  const { data: customCats } = useQuery({
    queryKey: ["expense-custom-categories"],
    queryFn: () => expensesService.getCustomCategories(),
  });

  const [amount, setAmount] = useState("");
  const [category, setCategory] = useState("RENT");
  const [customCategory, setCustomCategory] = useState(""); // label cuando OTHER
  const [paymentMethod, setPaymentMethod] = useState("transfer");
  const [expenseDate, setExpenseDate] = useState(nowStr);
  const [supplierId, setSupplierId] = useState("");
  const [description, setDescription] = useState("");
  const [isRecurring, setIsRecurring] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showNewSupplier, setShowNewSupplier] = useState(false);

  function reset() {
    setAmount("");
    setCategory("RENT");
    setCustomCategory("");
    setPaymentMethod("transfer");
    setExpenseDate(nowStr());
    setSupplierId("");
    setDescription("");
    setIsRecurring(false);
    setError(null);
    setShowNewSupplier(false);
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    if (!amount || parseFloat(amount) <= 0) {
      setError("Ingresá un monto válido mayor a 0.");
      return;
    }
    if (category === "OTHER" && !customCategory.trim()) {
      setError("Escribí el nombre de la categoría nueva.");
      return;
    }
    if (!supplierId) {
      setError("Seleccioná (o creá) un proveedor.");
      return;
    }
    // Categoría custom del tenant (value "__custom__:<label>") → OTHER + label.
    const isCustom = category.startsWith("__custom__:");
    const finalCategory = isCustom ? "OTHER" : category;
    const categoryLabel = isCustom
      ? category.slice("__custom__:".length)
      : category === "OTHER"
        ? customCategory.trim()
        : "";
    setSubmitting(true);
    await submit(
      "expense",
      {
        amount: parseFloat(amount),
        category: finalCategory,
        ...(categoryLabel ? { category_label: categoryLabel } : {}),
        expense_type: "OPEX", // la mercadería va en la pestaña Compra
        payment_method: paymentMethod,
        supplier_id: supplierId,
        expense_date: expenseDate,
        description: description.trim(),
        is_recurring: isRecurring,
      },
      {
        onSuccess: async () => {
          onToast({ type: "success", message: "Gasto registrado." });
          await queryClient.invalidateQueries({ queryKey: ["expense-custom-categories"] });
          reset();
        },
        onQueued: () => {
          onToast({
            type: "success",
            message: "Guardado sin conexión, se sincronizará al volver online.",
          });
          reset();
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
      <p className="rounded-lg border border-vk-border-w bg-vk-bg-light px-3 py-2 text-xs text-vk-text-secondary">
        Solo gastos operativos. Las compras de mercadería se cargan en la pestaña{" "}
        <strong>Compra</strong>.
      </p>

      <div className="grid grid-cols-2 gap-4">
        <Input
          label="Monto ($)"
          type="number"
          min={0}
          step="any"
          placeholder="0.00"
          value={amount}
          onChange={(e) => setAmount(e.target.value)}
        />
        <Field label="Categoría">
          <select
            value={category}
            onChange={(e) => setCategory(e.target.value)}
            className={selectClass}
          >
            {OPEX_CATEGORIES.map((c) => (
              <option key={c.value} value={c.value}>
                {c.label}
              </option>
            ))}
            {(customCats ?? []).map((label) => (
              <option key={`custom:${label}`} value={`__custom__:${label}`}>
                {label}
              </option>
            ))}
          </select>
        </Field>
      </div>

      {category === "OTHER" && (
        <Input
          label="Nueva categoría"
          placeholder="Ej: Veterinaria, Combustible…"
          maxLength={50}
          value={customCategory}
          onChange={(e) => setCustomCategory(e.target.value)}
        />
      )}

      <div className="grid grid-cols-2 gap-4">
        <Field label="Fecha y hora">
          <input
            type="datetime-local"
            max={nowStr()}
            value={expenseDate}
            onChange={(e) => setExpenseDate(e.target.value)}
            className={selectClass}
          />
        </Field>
        <Field label="Método de pago">
          <select
            value={paymentMethod}
            onChange={(e) => setPaymentMethod(e.target.value)}
            className={selectClass}
          >
            {PAYMENT_METHODS.map((m) => (
              <option key={m.value} value={m.value}>
                {m.label}
              </option>
            ))}
          </select>
        </Field>
      </div>

      <Field label="Proveedor">
        <select
          value={showNewSupplier ? NEW_OPTION : supplierId}
          onChange={(e) => {
            if (e.target.value === NEW_OPTION) {
              setShowNewSupplier(true);
            } else {
              setShowNewSupplier(false);
              setSupplierId(e.target.value);
            }
          }}
          className={selectClass}
        >
          <option value="">Seleccioná un proveedor…</option>
          {(suppliers ?? []).map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
          {online && <option value={NEW_OPTION}>+ Nuevo proveedor</option>}
        </select>
      </Field>

      {showNewSupplier && (
        <InlineSupplierCreate
          onCreated={async (id) => {
            setShowNewSupplier(false);
            setSupplierId(id);
            await queryClient.invalidateQueries({ queryKey: ["suppliers-list"] });
          }}
          onCancel={() => setShowNewSupplier(false)}
        />
      )}

      <Input
        label="Descripción (opcional)"
        type="text"
        placeholder="Ej: pago mensual alquiler"
        value={description}
        onChange={(e) => setDescription(e.target.value)}
      />

      <label className="flex cursor-pointer items-center gap-2.5">
        <input
          type="checkbox"
          checked={isRecurring}
          onChange={(e) => setIsRecurring(e.target.checked)}
          className="h-4 w-4 rounded border-vk-border-w accent-vk-blue"
        />
        <span className="text-sm text-vk-text-secondary">Gasto recurrente</span>
      </label>

      <FieldError msg={error ?? undefined} />

      <Button type="submit" size="sm" loading={submitting}>
        Registrar gasto
      </Button>
    </form>
  );
}

// ════════════════════════════ COMPRA ════════════════════════════

interface PurchaseLine {
  id: string;
  product_id: string; // "" o NEW_OPTION o un id
  category: string;
  name: string;
  sku: string;
  description: string;
  unit_cost: string;
  quantity: string;
  sale_price: string;
  update_price: boolean;
}

function newPurchaseLine(): PurchaseLine {
  return {
    id: crypto.randomUUID(),
    product_id: "",
    category: "",
    name: "",
    sku: "",
    description: "",
    unit_cost: "",
    quantity: "1",
    sale_price: "",
    update_price: false,
  };
}

function ProductTab({ onToast, online }: { onToast: (t: ToastState) => void; online: boolean }) {
  const { submit } = useOfflineSubmit();
  const queryClient = useQueryClient();
  const { data: products } = useProducts();
  const { data: suppliers } = useSuppliers();
  const { data: categories } = useProductCategories();

  const [supplierId, setSupplierId] = useState("");
  const [paymentMethod, setPaymentMethod] = useState("cash");
  const [transactionDate, setTransactionDate] = useState(nowStr);
  const [lines, setLines] = useState<PurchaseLine[]>([newPurchaseLine()]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showNewSupplier, setShowNewSupplier] = useState(false);

  // Crear categoría nueva (compartida por todas las líneas).
  const [newCat, setNewCat] = useState("");
  const [creatingCat, setCreatingCat] = useState(false);

  const productById = useMemo(() => {
    const m = new Map<string, ProductResponse>();
    for (const p of products ?? []) m.set(p.id, p);
    return m;
  }, [products]);

  function setLine(id: string, patch: Partial<PurchaseLine>) {
    setLines((prev) => prev.map((l) => (l.id === id ? { ...l, ...patch } : l)));
  }
  function onPickProduct(id: string, value: string) {
    if (value === NEW_OPTION || value === "") {
      setLine(id, { product_id: value, sale_price: "" });
      return;
    }
    const p = productById.get(value);
    setLine(id, {
      product_id: value,
      sale_price: p ? String(p.sale_price_ars) : "",
      category: p?.category ?? "",
    });
  }

  async function handleCreateCategory() {
    const label = newCat.trim();
    if (!label) return;
    setCreatingCat(true);
    try {
      await productsService.createCategory(label);
      await queryClient.invalidateQueries({ queryKey: ["product-categories"] });
      setNewCat("");
    } catch {
      onToast({ type: "error", message: "No se pudo crear la categoría." });
    }
    setCreatingCat(false);
  }

  const total = lines.reduce((s, l) => {
    const q = parseInt(l.quantity) || 0;
    const c = parseFloat(l.unit_cost) || 0;
    return s + q * c;
  }, 0);

  function reset() {
    setSupplierId("");
    setPaymentMethod("cash");
    setTransactionDate(nowStr());
    setLines([newPurchaseLine()]);
    setError(null);
    setShowNewSupplier(false);
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    if (!supplierId) {
      setError("Seleccioná (o creá) un proveedor.");
      return;
    }
    const payloadLines = [];
    for (const l of lines) {
      const isNew = l.product_id === NEW_OPTION;
      if (!l.product_id) {
        setError("Cada línea necesita un producto (existente o nuevo).");
        return;
      }
      const cost = parseFloat(l.unit_cost) || 0;
      const qty = parseInt(l.quantity) || 0;
      const price = parseFloat(l.sale_price) || 0;
      if (cost <= 0 || qty < 1 || price <= 0) {
        setError("Revisá costo (>0), cantidad (≥1) y precio de venta (>0) de cada línea.");
        return;
      }
      if (isNew && (!l.name.trim() || !l.category)) {
        setError("Para un producto nuevo: nombre y categoría son obligatorios.");
        return;
      }
      payloadLines.push({
        product_id: isNew ? null : l.product_id,
        name: isNew ? l.name.trim() : null,
        category: isNew ? l.category : null,
        sku: isNew ? l.sku.trim() || null : null,
        description: isNew ? l.description.trim() || null : null,
        unit_cost: round2(cost),
        quantity: qty,
        sale_price_ars: round2(price),
        update_price: !isNew && l.update_price,
      });
    }
    setSubmitting(true);
    await submit(
      "purchase",
      {
        supplier_id: supplierId,
        payment_method: paymentMethod,
        transaction_date: transactionDate,
        lines: payloadLines,
      },
      {
        onSuccess: () => {
          onToast({ type: "success", message: "Compra registrada (stock + gasto COGS)." });
          reset();
        },
        onQueued: () => {
          onToast({
            type: "success",
            message: "Guardado sin conexión, se sincronizará al volver online.",
          });
          reset();
        },
        onError: () => {
          onToast({ type: "error", message: "No se pudo registrar la compra. Revisá los datos." });
        },
      },
    );
    setSubmitting(false);
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      {/* Cabecera */}
      <div className="grid gap-4 sm:grid-cols-3">
        <Field label="Proveedor">
          <select
            value={showNewSupplier ? NEW_OPTION : supplierId}
            onChange={(e) => {
              if (e.target.value === NEW_OPTION) {
                setShowNewSupplier(true);
              } else {
                setShowNewSupplier(false);
                setSupplierId(e.target.value);
              }
            }}
            className={selectClass}
          >
            <option value="">Seleccioná…</option>
            {(suppliers ?? []).map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
            {online && <option value={NEW_OPTION}>+ Nuevo proveedor</option>}
          </select>
        </Field>
        <Field label="Fecha y hora">
          <input
            type="datetime-local"
            max={nowStr()}
            value={transactionDate}
            onChange={(e) => setTransactionDate(e.target.value)}
            className={selectClass}
          />
        </Field>
        <Field label="Método de pago">
          <select
            value={paymentMethod}
            onChange={(e) => setPaymentMethod(e.target.value)}
            className={selectClass}
          >
            {PAYMENT_METHODS.map((m) => (
              <option key={m.value} value={m.value}>
                {m.label}
              </option>
            ))}
          </select>
        </Field>
      </div>

      {showNewSupplier && (
        <InlineSupplierCreate
          onCreated={async (id) => {
            setShowNewSupplier(false);
            setSupplierId(id);
            await queryClient.invalidateQueries({ queryKey: ["suppliers-list"] });
          }}
          onCancel={() => setShowNewSupplier(false)}
        />
      )}

      {/* Crear categoría nueva (reutilizable) */}
      <div className="flex items-end gap-2">
        <div className="flex-1">
          <Input
            label="Nueva categoría de producto (opcional)"
            placeholder="Ej: Mascotas"
            value={newCat}
            onChange={(e) => setNewCat(e.target.value)}
          />
        </div>
        <Button
          type="button"
          variant="secondary"
          size="sm"
          loading={creatingCat}
          disabled={!newCat.trim() || !online}
          onClick={handleCreateCategory}
        >
          Crear categoría
        </Button>
      </div>

      {/* Líneas de compra */}
      <div className="space-y-3">
        <p className={labelClass}>Productos comprados</p>
        {lines.map((l) => {
          const isNew = l.product_id === NEW_OPTION;
          const existing = !isNew && l.product_id ? productById.get(l.product_id) : undefined;
          const cost = parseFloat(l.unit_cost) || 0;
          const qty = parseInt(l.quantity) || 0;
          const price = parseFloat(l.sale_price) || 0;
          const m = marginPct(cost, price);
          const optsForCat = l.category
            ? (products ?? []).filter((p) => (p.category ?? "") === l.category)
            : (products ?? []);
          return (
            <div key={l.id} className="space-y-2 rounded-lg border border-vk-border-w p-3">
              <div className="grid gap-2 sm:grid-cols-[1fr_1.4fr_auto]">
                <select
                  value={l.category}
                  onChange={(e) => setLine(l.id, { category: e.target.value })}
                  className={selectClass}
                >
                  <option value="">Categoría…</option>
                  {(categories ?? []).map((c) => (
                    <option key={c.code} value={c.code}>
                      {c.label}
                    </option>
                  ))}
                </select>
                <select
                  value={l.product_id}
                  onChange={(e) => onPickProduct(l.id, e.target.value)}
                  className={selectClass}
                >
                  <option value="">Producto…</option>
                  <option value={NEW_OPTION}>+ Producto nuevo</option>
                  {optsForCat.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  aria-label="Eliminar línea"
                  disabled={lines.length === 1}
                  onClick={() => setLines((prev) => prev.filter((x) => x.id !== l.id))}
                  className="inline-flex h-9 w-9 items-center justify-center rounded border border-vk-danger/30 text-vk-danger transition-colors hover:bg-vk-danger-bg disabled:opacity-40"
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>

              {isNew && (
                <div className="grid gap-2 sm:grid-cols-3">
                  <input
                    placeholder="Nombre del producto *"
                    value={l.name}
                    onChange={(e) => setLine(l.id, { name: e.target.value })}
                    className={selectClass}
                  />
                  <input
                    placeholder="SKU (opcional)"
                    value={l.sku}
                    onChange={(e) => setLine(l.id, { sku: e.target.value })}
                    className={selectClass}
                  />
                  <input
                    placeholder="Descripción (opcional)"
                    value={l.description}
                    onChange={(e) => setLine(l.id, { description: e.target.value })}
                    className={selectClass}
                  />
                </div>
              )}

              {existing && (
                <p className="text-xs text-vk-text-muted">
                  Stock actual: <strong>{existing.stock_units}</strong> · Umbral:{" "}
                  {existing.low_stock_threshold_units ?? "default del sistema"}
                </p>
              )}

              <div className="grid gap-2 sm:grid-cols-[0.9fr_0.6fr_0.9fr_auto]">
                <input
                  type="number"
                  min={0}
                  step="any"
                  placeholder="Costo unitario"
                  value={l.unit_cost}
                  onChange={(e) => setLine(l.id, { unit_cost: e.target.value })}
                  className={selectClass}
                />
                <input
                  type="number"
                  min={1}
                  step={1}
                  placeholder="Cantidad"
                  value={l.quantity}
                  onChange={(e) => setLine(l.id, { quantity: e.target.value })}
                  className={selectClass}
                />
                <input
                  type="number"
                  min={0}
                  step="any"
                  placeholder="Precio de venta"
                  value={l.sale_price}
                  onChange={(e) => setLine(l.id, { sale_price: e.target.value })}
                  className={selectClass}
                />
                <div className="flex items-center justify-end text-sm">
                  <span className="tabular-nums text-vk-text-secondary">
                    {m === null ? "—" : `Margen ${m}%`}
                  </span>
                </div>
              </div>

              <div className="flex items-center justify-between">
                {existing ? (
                  <label className="flex cursor-pointer items-center gap-2 text-xs text-vk-text-secondary">
                    <input
                      type="checkbox"
                      checked={l.update_price}
                      onChange={(e) => setLine(l.id, { update_price: e.target.checked })}
                      className="h-3.5 w-3.5 rounded border-vk-border-w accent-vk-blue"
                    />
                    Actualizar costo y precio del catálogo
                  </label>
                ) : (
                  <span />
                )}
                <span className="text-sm font-medium tabular-nums text-vk-text-primary">
                  Subtotal {fmtArs(qty * cost)}
                </span>
              </div>
            </div>
          );
        })}
        <button
          type="button"
          onClick={() => setLines((prev) => [...prev, newPurchaseLine()])}
          className="inline-flex items-center gap-1.5 text-sm font-medium text-vk-blue hover:underline"
        >
          <Plus className="h-4 w-4" /> Agregar producto
        </button>
      </div>

      <FieldError msg={error ?? undefined} />

      <div className="flex items-center justify-between border-t border-vk-border-w pt-3">
        <span className="text-sm text-vk-text-secondary">
          Total compra:{" "}
          <span className="font-semibold text-vk-text-primary">{fmtArs(total)}</span>
        </span>
        <Button type="submit" size="sm" loading={submitting}>
          Registrar compra
        </Button>
      </div>
    </form>
  );
}

// ════════════════════════════ ALTA DE PRODUCTO (catálogo) ════════════════════════════

function CatalogProductTab({
  onToast,
}: {
  onToast: (t: ToastState) => void;
}) {
  const { submit } = useOfflineSubmit();
  const { data: categories } = useProductCategories();

  const [name, setName] = useState("");
  const [category, setCategory] = useState("");
  const [salePrice, setSalePrice] = useState("");
  const [unitCost, setUnitCost] = useState("");
  const [stock, setStock] = useState("0");
  const [threshold, setThreshold] = useState("");
  const [sku, setSku] = useState("");
  const [description, setDescription] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function reset() {
    setName("");
    setCategory("");
    setSalePrice("");
    setUnitCost("");
    setStock("0");
    setThreshold("");
    setSku("");
    setDescription("");
    setError(null);
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    if (!name.trim()) {
      setError("El nombre es obligatorio.");
      return;
    }
    if (!salePrice || parseFloat(salePrice) <= 0) {
      setError("Ingresá un precio de venta válido (>0).");
      return;
    }
    if (unitCost && parseFloat(unitCost) <= 0) {
      setError("El costo, si lo cargás, debe ser mayor a 0.");
      return;
    }
    setSubmitting(true);
    await submit(
      "product",
      {
        name: name.trim(),
        sale_price_ars: round2(parseFloat(salePrice)),
        unit_cost_ars: unitCost ? round2(parseFloat(unitCost)) : null,
        stock_units: stock ? parseInt(stock) : 0,
        ...(threshold !== "" ? { low_stock_threshold_units: parseInt(threshold) } : {}),
        category: category.trim() || null,
        sku: sku.trim() || null,
        description: description.trim() || null,
      },
      {
        onSuccess: () => {
          onToast({ type: "success", message: "Producto dado de alta." });
          reset();
        },
        onQueued: () => {
          onToast({
            type: "success",
            message: "Guardado sin conexión, se sincronizará al volver online.",
          });
          reset();
        },
        onError: () => {
          onToast({ type: "error", message: "No se pudo dar de alta el producto." });
        },
      },
    );
    setSubmitting(false);
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <p className="rounded-lg border border-vk-border-w bg-vk-bg-light px-3 py-2 text-xs text-vk-text-secondary">
        Alta simple al catálogo (no registra compra ni mueve caja). Para cargar una compra
        con stock y costo, usá la pestaña <strong>Compra</strong>.
      </p>

      <Input
        label="Nombre del producto"
        placeholder="Ej: Agua mineral 500ml"
        value={name}
        onChange={(e) => setName(e.target.value)}
      />

      <div className="grid grid-cols-2 gap-4">
        <Field label="Categoría (opcional)">
          <select
            value={category}
            onChange={(e) => setCategory(e.target.value)}
            className={selectClass}
          >
            <option value="">Sin categoría</option>
            {(categories ?? []).map((c) => (
              <option key={c.code} value={c.code}>
                {c.label}
              </option>
            ))}
          </select>
        </Field>
        <Input
          label="Precio de venta ($)"
          type="number"
          min={0}
          step="any"
          placeholder="0.00"
          value={salePrice}
          onChange={(e) => setSalePrice(e.target.value)}
        />
      </div>

      <div className="grid grid-cols-2 gap-4">
        <Input
          label="Costo unitario ($, opcional)"
          type="number"
          min={0}
          step="any"
          placeholder="0.00"
          value={unitCost}
          onChange={(e) => setUnitCost(e.target.value)}
        />
        <Input
          label="Stock inicial"
          type="number"
          min={0}
          step={1}
          placeholder="0"
          value={stock}
          onChange={(e) => setStock(e.target.value)}
        />
      </div>

      <div className="grid grid-cols-2 gap-4">
        <Input
          label="Umbral de stock bajo (opcional)"
          type="number"
          min={0}
          step={1}
          placeholder="Default del sistema"
          value={threshold}
          onChange={(e) => setThreshold(e.target.value)}
        />
        <Input
          label="SKU (opcional)"
          placeholder="Ej: AGUA-500"
          maxLength={100}
          value={sku}
          onChange={(e) => setSku(e.target.value)}
        />
      </div>

      <Input
        label="Descripción (opcional)"
        placeholder="Ej: pack x6"
        value={description}
        onChange={(e) => setDescription(e.target.value)}
      />

      <FieldError msg={error ?? undefined} />

      <Button type="submit" size="sm" loading={submitting}>
        Dar de alta
      </Button>
    </form>
  );
}

// ── Inline create: cliente / proveedor ──────────────────────────────────────

function InlineCustomerCreate({
  onCreated,
  onCancel,
}: {
  onCreated: (id: string) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState("");
  const [lastName, setLastName] = useState("");
  const [doc, setDoc] = useState("");
  const [phone, setPhone] = useState("");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function create() {
    setErr(null);
    if (!name.trim() || !lastName.trim() || !doc.trim() || !phone.trim()) {
      setErr("Completá nombre, apellido, DNI/CUIT y teléfono.");
      return;
    }
    const digits = doc.replace(/\D/g, "");
    const payload = {
      name: name.trim(),
      last_name: lastName.trim(),
      phone: phone.trim(),
      ...(digits.length > 8 ? { cuit: digits } : { dni: digits }),
    };
    setSaving(true);
    try {
      const created = await customersService.createCustomer(payload);
      onCreated(created.id);
    } catch {
      setErr("No se pudo crear el cliente. Revisá los datos.");
    }
    setSaving(false);
  }

  return (
    <div className="space-y-2 rounded-lg border border-vk-blue/30 bg-vk-blue/5 p-3">
      <p className="text-xs font-medium text-vk-text-secondary">Nuevo cliente</p>
      <div className="grid gap-2 sm:grid-cols-2">
        <input placeholder="Nombre *" value={name} onChange={(e) => setName(e.target.value)} className={selectClass} />
        <input placeholder="Apellido *" value={lastName} onChange={(e) => setLastName(e.target.value)} className={selectClass} />
        <input placeholder="DNI o CUIT *" value={doc} onChange={(e) => setDoc(e.target.value)} className={selectClass} />
        <input placeholder="Teléfono *" value={phone} onChange={(e) => setPhone(e.target.value)} className={selectClass} />
      </div>
      <FieldError msg={err ?? undefined} />
      <div className="flex justify-end gap-2">
        <Button type="button" variant="secondary" size="sm" onClick={onCancel}>
          Cancelar
        </Button>
        <Button type="button" size="sm" loading={saving} onClick={create}>
          Crear cliente
        </Button>
      </div>
    </div>
  );
}

function InlineSupplierCreate({
  onCreated,
  onCancel,
}: {
  onCreated: (id: string) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function create() {
    setErr(null);
    if (!name.trim()) {
      setErr("Ingresá el nombre del proveedor.");
      return;
    }
    setSaving(true);
    try {
      const created = await suppliersService.createSupplier({ name: name.trim() });
      onCreated(created.id);
    } catch {
      setErr("No se pudo crear el proveedor.");
    }
    setSaving(false);
  }

  return (
    <div className="space-y-2 rounded-lg border border-vk-blue/30 bg-vk-blue/5 p-3">
      <p className="text-xs font-medium text-vk-text-secondary">Nuevo proveedor</p>
      <input
        placeholder="Nombre / razón social *"
        value={name}
        onChange={(e) => setName(e.target.value)}
        className={selectClass}
      />
      <FieldError msg={err ?? undefined} />
      <div className="flex justify-end gap-2">
        <Button type="button" variant="secondary" size="sm" onClick={onCancel}>
          Cancelar
        </Button>
        <Button type="button" size="sm" loading={saving} onClick={create}>
          Crear proveedor
        </Button>
      </div>
    </div>
  );
}

// ── ManualEntrySection ────────────────────────────────────────────────────────

type ActiveTab = "sale" | "expense" | "product" | "catalog";

const TABS: { key: ActiveTab; label: string }[] = [
  { key: "sale", label: "Registrar venta" },
  { key: "expense", label: "Registrar gasto" },
  { key: "product", label: "Registrar compra" },
  { key: "catalog", label: "Alta de producto" },
];

/**
 * Carga manual transaccional (venta multi-línea que descuenta stock, gasto OPEX,
 * compra de mercadería que crea stock + COGS). Se renderiza dentro del Modal
 * flotante de `ManualEntryLauncher`. Mantiene custom fields del vertical en gasto.
 */
export function ManualEntrySection({
  onDirtyChange,
}: {
  onDirtyChange?: (dirty: boolean) => void;
}) {
  const [activeTab, setActiveTab] = useState<ActiveTab>("sale");
  const [toast, setToast] = useState<ToastState>(null);
  const [dirty, setDirty] = useState(false);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pending = useOfflineQueueCount();
  const online = useOnlineStatus();

  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);

  function handleToast(t: ToastState) {
    if (toastTimer.current) clearTimeout(toastTimer.current);
    setToast(t);
    toastTimer.current = setTimeout(() => setToast(null), 3_000);
    // Una carga exitosa resetea el form → ya no hay datos sin guardar.
    if (t?.type === "success") setDirty(false);
  }

  function switchTab(next: ActiveTab) {
    if (next === activeTab) return;
    if (
      dirty &&
      !window.confirm("Tenés datos sin guardar en esta pestaña. ¿Cambiar y descartarlos?")
    ) {
      return;
    }
    setDirty(false);
    setActiveTab(next);
  }

  useEffect(() => {
    return () => {
      if (toastTimer.current) clearTimeout(toastTimer.current);
    };
  }, []);

  return (
    <div>
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

      <div className="mb-6 flex gap-1 rounded-lg border border-vk-border-w bg-vk-bg-light p-1">
        {TABS.map((tab) => (
          <button
            key={tab.key}
            type="button"
            onClick={() => switchTab(tab.key)}
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

      {toast && (
        <div className="mb-4">
          <Toast toast={toast} />
        </div>
      )}

      {/* onChangeCapture marca "datos sin guardar": cubre inputs/selects/checkboxes
          de cualquier tab. Se limpia en submit exitoso o al cambiar de pestaña. */}
      <div onChangeCapture={() => setDirty(true)}>
        {activeTab === "sale" && <SaleTab onToast={handleToast} online={online} />}
        {activeTab === "expense" && <ExpenseTab onToast={handleToast} online={online} />}
        {activeTab === "product" && <ProductTab onToast={handleToast} online={online} />}
        {activeTab === "catalog" && <CatalogProductTab onToast={handleToast} />}
      </div>
    </div>
  );
}
