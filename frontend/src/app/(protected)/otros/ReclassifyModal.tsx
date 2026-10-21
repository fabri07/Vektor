"use client";

// Vive en su PROPIO módulo y no dentro de ``page.tsx``: en el App Router un
// ``page.tsx`` solo puede exportar el default y los campos que Next reconoce
// (``metadata``, ``generateMetadata``, ...). Exportar un componente de ahí compila con
// ``tsc`` pero rompe ``next build`` con "is not a valid Page export field" — que es
// exactamente como se coló, exportado únicamente para que lo importara su test.
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link2, Package, Plus, Trash2 } from "lucide-react";
import { Modal } from "@/components/ui/Modal";
import type {
  ProductMatchCandidate,
  ReclassifyEntityType,
  ResolvePurchasePayload,
  UnclassifiedRecordResponse,
} from "@/services/others.service";
import { productsService, type ProductCategoryOption } from "@/services/products.service";
import { ALL_CATEGORIES, CATEGORY_LABELS } from "@/lib/expenseCategories";
import { PurchaseTermsFields } from "@/features/purchases/PurchaseTermsFields";
import {
  EMPTY_TERMS,
  termsError,
  termsPayload,
  type PurchaseTerms,
} from "@/features/purchases/purchaseTerms";
import { prefill, rowPreview } from "./helpers";

/** B·5b: «Compra de mercadería» es un destino propio: se resuelve con artículos. */
type Destino = ReclassifyEntityType | "purchase";

interface LineaDeCompra {
  productId: string;
  quantity: string;
  amount: string;
}

const NUEVA_LINEA: LineaDeCompra = { productId: "", quantity: "1", amount: "" };

/** Categorías de gasto que se pueden elegir: mercadería no (va como compra). */
const GASTO_CATEGORIES = ALL_CATEGORIES.filter((c) => c !== "INVENTORY");

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
  onResolvePurchase: (payload: ResolvePurchasePayload) => void;
}) {
  const [entityType, setEntityType] = useState<Destino>("expense");
  const [lineas, setLineas] = useState<LineaDeCompra[]>([NUEVA_LINEA]);
  const [supplierName, setSupplierName] = useState("");
  const [terms, setTerms] = useState<PurchaseTerms>(EMPTY_TERMS);
  // Búsqueda de artículos: un negocio puede tener miles y un <select> solo no escala.
  const [busqueda, setBusqueda] = useState("");
  // El error de la compra se muestra después del primer intento, no al abrir.
  const [intento, setIntento] = useState(false);
  const [amount, setAmount] = useState("");
  const [date, setDate] = useState("");
  const [text, setText] = useState("");
  const [category, setCategory] = useState("OTHER");
  const [productCategory, setProductCategory] = useState("");
  const [paymentMethod, setPaymentMethod] = useState("cash");
  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  // B·5e: una venta lleva producto (y su compra previa); sin él el servidor la
  // rechaza. Se elige acá, con la misma búsqueda que la compra.
  const [ventaProducto, setVentaProducto] = useState("");
  const [ventaCantidad, setVentaCantidad] = useState("1");

  const { data: productos = [], isError: productosError } = useQuery({
    queryKey: ["products-list"],
    queryFn: () => productsService.getAllProducts({ is_active: true }),
    enabled: !!record && (entityType === "purchase" || entityType === "sale"),
    staleTime: 60 * 1000,
  });

  useEffect(() => {
    if (!record) return;
    const pre = prefill(record);
    const entity = record.suggested_entity ?? "expense";
    // Una compra de mercadería (con candidatos, o con categoría mercadería) se
    // abre directamente como compra: va con sus artículos.
    const esCompra =
      entity === "expense" &&
      ((record.match_candidates?.length ?? 0) > 0 || record.suggested_category === "INVENTORY");
    setEntityType(esCompra ? "purchase" : entity);
    setLineas([
      {
        productId: record.match_candidates?.length === 1 ? record.match_candidates[0]!.id : "",
        quantity: pre.quantity.replace(/[^\d]/g, "") || "1",
        amount: pre.amount.replace(/[^\d.,]/g, ""),
      },
    ]);
    setSupplierName("");
    setTerms(EMPTY_TERMS);
    setBusqueda("");
    setIntento(false);
    setAmount(pre.amount.replace(/[^\d.,]/g, ""));
    setDate(pre.date.slice(0, 10));
    setText(pre.text);
    // Prellenar con la categoría recomendada por el backend según el destino.
    setCategory(
      entity === "expense" && record.suggested_category && record.suggested_category !== "INVENTORY"
        ? record.suggested_category
        : "OTHER",
    );
    setProductCategory(
      entity === "product" && record.suggested_category ? record.suggested_category : "",
    );
    setPaymentMethod("cash");
    setEmail("");
    setPhone("");
    setVentaProducto("");
    setVentaCantidad(pre.quantity.replace(/[^\d]/g, "") || "1");
    // Lo que decía el archivo (B·5e lo guarda en `producto`): arranca la búsqueda.
    if (entity === "sale") setBusqueda(String(record.row_data?.producto ?? pre.text ?? ""));
  }, [record]);

  if (!record) return null;

  // F2-T2b: candidatos de producto existente para VINCULAR (solo filas de producto
  // ambiguas/en conflicto). Fuera de ese caso el backend manda null.
  const isPurchaseResolution = entityType === "purchase";
  const candidates = record.match_candidates ?? [];
  const setLinea = (i: number, patch: Partial<LineaDeCompra>) =>
    setLineas((prev) => prev.map((l, j) => (j === i ? { ...l, ...patch } : l)));
  const lineasValidas = lineas.every(
    (l) => l.productId && Number(l.quantity) >= 1 && Number(l.amount.replace(",", ".")) > 0,
  );
  const productosRepetidos =
    new Set(lineas.map((l) => l.productId)).size !== lineas.length;
  const errorDeCompra = !lineasValidas
    ? "Elegí el artículo, la cantidad y el importe de cada línea."
    : productosRepetidos
      ? "Hay un artículo repetido: unificá la cantidad en una línea."
      : termsError(terms);

  // Cliente y Proveedor son entidades de contacto: solo nombre + email/teléfono.
  const isContact = entityType === "customer" || entityType === "supplier";
  const inputCls = "rounded border border-vk-border-w px-3 py-2";
  // F6: la fila llegó a /otros porque no tiene fecha confiable. Al resolverla NO
  // se inventa "hoy": la fecha es obligatoria para venta/gasto. El <input> es
  // required, pero lo validamos explícito (defensa) y nunca construimos un fallback.
  const needsDate = entityType === "sale" || entityType === "expense" || entityType === "purchase";
  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    if (needsDate && !date) return;
    const num = Number(amount.replace(",", "."));
    const isoDate = `${date}T00:00:00`;
    if (entityType === "purchase") {
      setIntento(true);
      if (errorDeCompra) return;
      onResolvePurchase({
        lines: lineas.map((l) => ({
          product_id: l.productId,
          quantity: Number(l.quantity),
          amount: Number(l.amount.replace(",", ".")),
        })),
        transaction_date: isoDate,
        payment_method: paymentMethod,
        ...(supplierName.trim() ? { supplier_name: supplierName.trim() } : {}),
        ...termsPayload(terms),
      });
      return;
    }
    if (entityType === "sale") {
      if (!ventaProducto) {
        setIntento(true);
        return;
      }
      onSave("sale", {
        amount: num,
        transaction_date: isoDate,
        payment_method: paymentMethod,
        product_id: ventaProducto,
        quantity: Math.max(1, Number(ventaCantidad) || 1),
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
                      disabled={saving}
                      onClick={() => {
                        if (isPurchaseResolution) {
                          // B·5b: el candidato es el artículo de la primera línea; la
                          // compra se registra con el botón del formulario, con fecha.
                          setLinea(0, { productId: c.id });
                        } else {
                          onLink(c.id);
                        }
                      }}
                      className="inline-flex shrink-0 items-center gap-1.5 rounded border border-vektor-teal/40 px-2.5 py-1.5 text-xs font-medium text-vektor-teal transition-colors hover:bg-vektor-teal/10 disabled:opacity-50"
                    >
                      <Link2 className="h-3.5 w-3.5" />
                      {isPurchaseResolution ? "Usar este artículo" : "Vincular"}
                    </button>
                  </li>
                );
              })}
            </ul>
            <div className="flex items-center gap-1.5 pt-1 text-xs text-vk-text-muted">
              <Package className="h-3.5 w-3.5" />
              {isPurchaseResolution
                ? "Completá los artículos, la fecha y el importe antes de registrar la compra."
                : "…o completá el formulario de abajo para crear un producto nuevo."}
            </div>
          </div>
        ) : null}

        <label className="grid gap-1 text-sm text-vektor-body">
          Importar como
          <select
            className={inputCls}
            value={entityType}
            onChange={(e) => setEntityType(e.target.value as Destino)}
          >
            <option value="sale">Venta</option>
            {/* Sólo una fila de gasto (o sin sugerencia) se resuelve como compra. */}
            {record.suggested_entity === "expense" || record.suggested_entity === null ? (
              <option value="purchase">Compra de mercadería</option>
            ) : null}
            <option value="expense">Gasto</option>
            <option value="product">Producto</option>
            <option value="customer">Cliente</option>
            <option value="supplier">Proveedor</option>
          </select>
        </label>
        {!isContact && !isPurchaseResolution ? (
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
        {entityType === "sale" ? (
          <div className="grid gap-2">
            <label className="grid gap-1 text-sm text-vektor-body">
              ¿Qué producto se vendió?
              <input
                className={inputCls}
                placeholder="Buscar producto"
                value={busqueda}
                onChange={(e) => setBusqueda(e.target.value)}
              />
            </label>
            <div className="grid grid-cols-[1fr_6rem] gap-2">
              <select
                aria-label="Producto vendido"
                className={inputCls}
                value={ventaProducto}
                onChange={(e) => setVentaProducto(e.target.value)}
              >
                <option value="">Elegí el producto</option>
                {productos
                  .filter(
                    (p) =>
                      p.id === ventaProducto ||
                      !busqueda.trim() ||
                      p.name.toLowerCase().includes(busqueda.trim().toLowerCase()),
                  )
                  .map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
              </select>
              <input
                aria-label="Cantidad vendida"
                className={inputCls}
                type="number"
                min={1}
                step={1}
                value={ventaCantidad}
                onChange={(e) => setVentaCantidad(e.target.value)}
              />
            </div>
            {intento && !ventaProducto ? (
              <p role="alert" className="text-xs text-vk-danger">
                Elegí qué producto se vendió: sin producto no hay compra previa ni costo.
              </p>
            ) : (
              <p className="text-xs text-vk-text-muted">
                El producto tiene que tener una compra o un stock inicial a esa fecha.
              </p>
            )}
          </div>
        ) : null}
        {isPurchaseResolution ? (
          <div className="grid gap-3">
            <label className="grid gap-1 text-sm text-vektor-body">
              Fecha de la compra
              <input
                className={inputCls}
                type="date"
                required
                value={date}
                onChange={(e) => setDate(e.target.value)}
              />
            </label>
            <p className="text-xs text-vk-text-muted">
              ¿Qué artículos compraste? Una compra de mercadería sin artículos no se puede usar
              para calcular costos ni márgenes.
            </p>
            <label className="grid gap-1 text-sm text-vektor-body">
              Buscar artículo
              <input
                className={inputCls}
                value={busqueda}
                placeholder="Nombre del artículo"
                onChange={(e) => setBusqueda(e.target.value)}
              />
            </label>
            {productosError ? (
              <p className="text-xs text-vk-danger">
                No pudimos cargar tus artículos. Cerrá y volvé a abrir para reintentar.
              </p>
            ) : null}
            {lineas.map((l, i) => (
              <div key={i} className="grid grid-cols-[1fr_5rem_7rem_2rem] items-end gap-2">
                <label className="grid gap-1 text-sm text-vektor-body">
                  Artículo
                  <select
                    className={inputCls}
                    aria-label={`Artículo de la línea ${i + 1}`}
                    value={l.productId}
                    onChange={(e) => setLinea(i, { productId: e.target.value })}
                  >
                    <option value="">Elegí un artículo…</option>
                    {productos
                      .filter(
                        (p) =>
                          p.id === l.productId ||
                          !busqueda.trim() ||
                          p.name.toLowerCase().includes(busqueda.trim().toLowerCase()),
                      )
                      .map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name}
                      </option>
                    ))}
                    {candidates
                      .filter((c) => !productos.some((p) => p.id === c.id))
                      .map((c) => (
                        <option key={c.id} value={c.id}>
                          {c.name ?? "Producto sugerido"}
                        </option>
                      ))}
                  </select>
                </label>
                <label className="grid gap-1 text-sm text-vektor-body">
                  Cantidad
                  <input
                    className={inputCls}
                    aria-label={`Cantidad de la línea ${i + 1}`}
                    type="number"
                    min={1}
                    step={1}
                    value={l.quantity}
                    onChange={(e) => setLinea(i, { quantity: e.target.value })}
                  />
                </label>
                <label className="grid gap-1 text-sm text-vektor-body">
                  Importe
                  <input
                    className={inputCls}
                    aria-label={`Importe de la línea ${i + 1}`}
                    type="number"
                    min={0}
                    step="0.01"
                    value={l.amount}
                    onChange={(e) => setLinea(i, { amount: e.target.value })}
                  />
                </label>
                <button
                  type="button"
                  aria-label={`Quitar la línea ${i + 1}`}
                  disabled={lineas.length === 1}
                  onClick={() => setLineas((prev) => prev.filter((_, j) => j !== i))}
                  className="mb-1 inline-flex h-8 w-8 items-center justify-center rounded border border-vk-border-w disabled:opacity-40"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </div>
            ))}
            <button
              type="button"
              onClick={() => setLineas((prev) => [...prev, NUEVA_LINEA])}
              className="inline-flex w-fit items-center gap-1.5 rounded border border-vk-border-w px-2.5 py-1.5 text-xs text-vk-text-primary"
            >
              <Plus className="h-3.5 w-3.5" /> Agregar artículo
            </button>
            <label className="grid gap-1 text-sm text-vektor-body">
              Proveedor (opcional)
              <input
                className={inputCls}
                value={supplierName}
                onChange={(e) => setSupplierName(e.target.value)}
              />
            </label>
            <PurchaseTermsFields value={terms} onChange={setTerms} />
            {intento && errorDeCompra ? (
              <p id="error-de-compra" role="alert" className="text-xs text-vk-danger">
                {errorDeCompra}
              </p>
            ) : null}
            {!date ? (
              <p className="text-xs text-vk-text-muted">Falta la fecha de la compra.</p>
            ) : null}
          </div>
        ) : null}
        {!isPurchaseResolution ? (
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
        ) : null}
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
              {GASTO_CATEGORIES.map((cat) => (
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
            {saving ? "Importando..." : isPurchaseResolution ? "Registrar compra" : "Importar"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
