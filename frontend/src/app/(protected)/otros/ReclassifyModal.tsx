"use client";

// Vive en su PROPIO módulo y no dentro de ``page.tsx``: en el App Router un
// ``page.tsx`` solo puede exportar el default y los campos que Next reconoce
// (``metadata``, ``generateMetadata``, ...). Exportar un componente de ahí compila con
// ``tsc`` pero rompe ``next build`` con "is not a valid Page export field" — que es
// exactamente como se coló, exportado únicamente para que lo importara su test.
import { useEffect, useState } from "react";
import { Link2, Package } from "lucide-react";
import { Modal } from "@/components/ui/Modal";
import type {
  ProductMatchCandidate,
  ReclassifyEntityType,
  UnclassifiedRecordResponse,
} from "@/services/others.service";
import type { ProductCategoryOption } from "@/services/products.service";
import { ALL_CATEGORIES, CATEGORY_LABELS } from "@/lib/expenseCategories";
import { prefill, rowPreview } from "./helpers";

function candidateLabel(c: ProductMatchCandidate): string {
  const parts: string[] = [];
  if (c.sku) parts.push(`SKU ${c.sku}`);
  if (c.barcode) parts.push(`EAN ${c.barcode}`);
  return parts.join(" · ");
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
  // F6: la fila llegó a /otros porque no tiene fecha confiable. Al resolverla NO
  // se inventa "hoy": la fecha es obligatoria para venta/gasto. El <input> es
  // required, pero lo validamos explícito (defensa) y nunca construimos un fallback.
  const needsDate = entityType === "sale" || entityType === "expense";
  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    if (needsDate && !date) return;
    const num = Number(amount.replace(",", "."));
    const isoDate = `${date}T00:00:00`;
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
                      // El botón NO es submit, así que el `required` del <input date>
                      // no lo bloquea: sin este guard, resolver una compra sin fecha
                      // caía al fallback "hoy" (F6). Fecha obligatoria y sin fallback.
                      disabled={saving || (isPurchaseResolution && !date)}
                      onClick={() => {
                        if (isPurchaseResolution) {
                          if (!date) return;
                          const parsedUnitCost = unitCost
                            ? Number(unitCost.replace(",", "."))
                            : undefined;
                          onResolvePurchase(c.id, {
                            amount: Number(amount.replace(",", ".")),
                            quantity: Number(quantity),
                            ...(parsedUnitCost !== undefined
                              ? { unitCost: parsedUnitCost }
                              : {}),
                            transactionDate: `${date}T00:00:00`,
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
            // Fecha obligatoria para venta/gasto: sin ella no se puede importar
            // (no se inventa "hoy" — F6). Refuerza el `required` del <input>.
            disabled={saving || (needsDate && !date)}
            className="rounded bg-vk-blue px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
          >
            {saving ? "Importando..." : "Importar"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
