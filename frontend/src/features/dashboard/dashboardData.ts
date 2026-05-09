import type { ExpenseEntryResponse } from "@/services/expenses.service";
import type { ProductResponse } from "@/services/products.service";
import type { SaleEntryResponse } from "@/services/sales.service";
import type { HealthScoreV2Response } from "@/types/api";

export const KPI_TOOLTIP_COPY: Record<string, string> = {
  Caja:
    "Caja: cuánto dinero está entrando y cómo se reparte entre distintos medios de cobro.",
  Margen:
    "Margen: qué parte de cada venta se transforma en ganancia antes de gastos fijos.",
  Stock:
    "Stock: cómo está distribuido tu inventario entre productos disponibles, bajos, agotados y en camino.",
  Proveedores:
    "Proveedores: quiénes concentran tus compras y qué nivel de dependencia tenés con cada uno.",
  "Riesgo principal":
    "Este es el factor que más amenaza la salud de tu negocio en este momento.",
};

export interface CashMethodRow {
  method: string;
  amount: number;
  percentage: number;
  colorClass: string;
}

export interface MarginCategoryRow {
  category: string;
  margin: number;
  tone: "green" | "amber" | "red";
}

export interface StockStatusRow {
  id: "ok" | "low" | "out" | "incoming";
  label: string;
  count: number;
  colorClass: string;
  description: string;
}

export interface SupplierSummary {
  name: string;
  totalPurchased: number;
  lastOrderDate: string | null;
  averageOrderValue: number;
  paymentTerms: string;
  overdueInvoicesCount: number;
}

export interface LineMetricOption {
  value: "caja" | "ventas" | "margen" | "stock";
  label: string;
  title: string;
}

export interface CompareOption {
  value: "categoria" | "proveedor" | "metodo" | "dia";
  label: string;
  title: string;
}

export interface DistributionOption {
  value: "ventasCategoria" | "stockEstado" | "comprasProveedor" | "cajaMetodo";
  label: string;
  title: string;
}

export const LINE_METRIC_OPTIONS: LineMetricOption[] = [
  { value: "caja", label: "Evolución de caja", title: "¿Cómo evolucionó tu caja este mes?" },
  { value: "ventas", label: "Ventas totales", title: "¿Cómo evolucionaron tus ventas?" },
  { value: "margen", label: "Margen promedio", title: "¿Cómo se movió tu margen promedio?" },
  { value: "stock", label: "Score de stock", title: "¿Cómo evolucionó tu score de stock?" },
];

export const COMPARE_OPTIONS: CompareOption[] = [
  { value: "categoria", label: "Categoría de producto", title: "¿Qué categoría mueve más?" },
  { value: "proveedor", label: "Proveedor", title: "¿Qué proveedores concentran tus compras?" },
  { value: "metodo", label: "Método de pago", title: "¿Cómo te pagan más seguido?" },
  { value: "dia", label: "Día de semana", title: "¿Qué días concentran más movimiento?" },
];

export const DISTRIBUTION_OPTIONS: DistributionOption[] = [
  { value: "ventasCategoria", label: "Ventas por categoría", title: "¿Cómo se distribuyen tus ventas por categoría?" },
  { value: "stockEstado", label: "Stock por estado", title: "¿Cómo está distribuido tu inventario?" },
  { value: "comprasProveedor", label: "Compras por proveedor", title: "¿Cómo se reparten tus compras entre proveedores?" },
  { value: "cajaMetodo", label: "Caja por método", title: "¿Cómo se distribuye tu caja por medio de pago?" },
];

const PAYMENT_METHODS = ["Efectivo", "Débito", "Crédito", "Cheque", "Pagaré"] as const;

const METHOD_COLORS: Record<(typeof PAYMENT_METHODS)[number], string> = {
  Efectivo: "border-l-vektor-teal",
  Débito: "border-l-vektor-blue",
  Crédito: "border-l-vk-warning",
  Cheque: "border-l-vk-info",
  Pagaré: "border-l-vektor-red",
};

const WEEKDAY_LABELS = ["Dom", "Lun", "Mar", "Mié", "Jue", "Vie", "Sáb"];

export function formatARS(value: number): string {
  return new Intl.NumberFormat("es-AR", {
    style: "currency",
    currency: "ARS",
    maximumFractionDigits: 0,
  }).format(value);
}

export function formatARSCompact(value: number): string {
  if (Math.abs(value) >= 1_000_000) {
    return `$${(value / 1_000_000).toFixed(1)}M`;
  }
  if (Math.abs(value) >= 1_000) {
    return `$${(value / 1_000).toFixed(0)}k`;
  }
  return formatARS(value);
}

export function formatPercent(value: number, digits = 1): string {
  return `${value.toFixed(digits)}%`;
}

export function formatSignedPercent(value: number): string {
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(1)}%`;
}

export function formatShortDate(date: string | null): string {
  if (!date) return "—";
  return new Date(date).toLocaleDateString("es-AR", {
    day: "numeric",
    month: "short",
  });
}

export function normalizePaymentMethod(raw: string | null | undefined): (typeof PAYMENT_METHODS)[number] {
  const value = raw?.toLowerCase() ?? "";
  if (value.includes("deb")) return "Débito";
  if (value.includes("cred")) return "Crédito";
  if (value.includes("cheq")) return "Cheque";
  if (value.includes("paga")) return "Pagaré";
  return "Efectivo";
}

export function buildCashBreakdown(entries: SaleEntryResponse[]): {
  total: number;
  rows: CashMethodRow[];
} {
  const total = entries.reduce((sum, entry) => sum + entry.amount, 0);

  if (entries.length === 0 || total === 0) {
    return { total: 0, rows: [] };
  }

  const buckets = PAYMENT_METHODS.reduce<Record<(typeof PAYMENT_METHODS)[number], number>>((acc, method) => {
    acc[method] = 0;
    return acc;
  }, {} as Record<(typeof PAYMENT_METHODS)[number], number>);

  for (const entry of entries) {
    const method = normalizePaymentMethod(entry.payment_method);
    buckets[method] += entry.amount;
  }

  const rows = PAYMENT_METHODS.map((method) => ({
    method,
    amount: buckets[method],
    percentage: total > 0 ? (buckets[method] / total) * 100 : 0,
    colorClass: METHOD_COLORS[method],
  }));

  return { total, rows };
}

export function buildMarginBreakdown(products: ProductResponse[]): MarginCategoryRow[] {
  const categories = new Map<string, { total: number; count: number }>();

  for (const product of products) {
    if (product.margin_pct == null) continue;
    const category = product.category?.trim() || "Sin categoría";
    const current = categories.get(category) ?? { total: 0, count: 0 };
    current.total += product.margin_pct;
    current.count += 1;
    categories.set(category, current);
  }

  if (categories.size === 0) {
    return [];
  }

  return [...categories.entries()]
    .map(([category, summary]) => {
      const margin = summary.total / summary.count;
      return {
        category,
        margin,
        tone: margin > 25 ? "green" : margin >= 10 ? "amber" : "red",
      } satisfies MarginCategoryRow;
    })
    .sort((a, b) => b.margin - a.margin);
}

export function buildStockStatuses(products: ProductResponse[]): StockStatusRow[] {
  const inStock = products
    .filter((product) => product.stock_units > 0 && !product.is_low_stock)
    .reduce((sum, product) => sum + product.stock_units, 0);
  const lowStock = products
    .filter((product) => product.stock_units > 0 && product.is_low_stock)
    .reduce((sum, product) => sum + product.stock_units, 0);
  const outOfStock = products.filter((product) => product.stock_units === 0).length;

  return [
    {
      id: "ok",
      label: "En stock",
      count: inStock,
      colorClass: "bg-vektor-teal text-vektor-white",
      description: "Productos disponibles para vender ahora mismo.",
    },
    {
      id: "low",
      label: "Pocas unidades",
      count: lowStock,
      colorClass: "bg-vektor-amber text-vektor-night",
      description: "Menos de tu umbral recomendado. Conviene reponer pronto.",
    },
    {
      id: "out",
      label: "Sin stock",
      count: outOfStock,
      colorClass: "bg-vektor-red text-vektor-white",
      description: "Productos agotados. Perdés ventas si no reponés.",
    },
    {
      id: "incoming",
      label: "En camino",
      count: 0,
      colorClass: "bg-vektor-blue text-vektor-white",
      // TODO: requires purchase_order status in API.
      description: "Comprado al proveedor pero aún no recibido en tu local.",
    },
  ];
}

export function buildSupplierSummaries(expenses: ExpenseEntryResponse[]): SupplierSummary[] {
  const suppliers = new Map<string, { total: number; count: number; lastDate: string | null }>();

  for (const expense of expenses) {
    const supplier = expense.supplier_name?.trim();
    if (!supplier) continue;
    const current = suppliers.get(supplier) ?? { total: 0, count: 0, lastDate: null };
    current.total += expense.amount;
    current.count += 1;
    if (!current.lastDate || new Date(expense.transaction_date) > new Date(current.lastDate)) {
      current.lastDate = expense.transaction_date;
    }
    suppliers.set(supplier, current);
  }

  if (suppliers.size === 0) {
    return [];
  }

  return [...suppliers.entries()]
    .map(([name, summary]) => ({
      name,
      totalPurchased: summary.total,
      lastOrderDate: summary.lastDate,
      averageOrderValue: summary.total / summary.count,
      // TODO: enrich with real payment terms and overdue invoice count from supplier API.
      paymentTerms: summary.total > 1_000_000 ? "30 días" : "15 días",
      overdueInvoicesCount: summary.total > 1_500_000 ? 1 : 0,
    }))
    .sort((a, b) => b.totalPurchased - a.totalPurchased);
}

function dateRange(points: number): Date[] {
  const today = new Date();
  return Array.from({ length: points }, (_, index) => {
    const value = new Date(today);
    value.setDate(today.getDate() - (points - 1 - index));
    return value;
  });
}

function sameDay(date: string, target: Date): boolean {
  const parsed = new Date(date);
  return parsed.toDateString() === target.toDateString();
}

export function buildLineSeries(
  metric: LineMetricOption["value"],
  sales: SaleEntryResponse[],
  expenses: ExpenseEntryResponse[],
  products: ProductResponse[],
  granularity: "daily" | "weekly" | "monthly",
  scoreHistory?: HealthScoreV2Response[],
): Array<{ label: string; value: number | null; change: number }> {
  const points = granularity === "daily" ? 14 : granularity === "weekly" ? 10 : 6;
  const dates = dateRange(points);
  const totalStock = products.reduce((sum, product) => sum + product.stock_units, 0);

  let previous = 0;

  return dates.map((date, index) => {
    let value: number | null = null;

    if (metric === "ventas") {
      value = sales
        .filter((entry) => sameDay(entry.transaction_date, date))
        .reduce((sum, entry) => sum + entry.amount, 0);
    } else if (metric === "caja") {
      const salesTotal = sales
        .filter((entry) => sameDay(entry.transaction_date, date))
        .reduce((sum, entry) => sum + entry.amount, 0);
      const expensesTotal = expenses
        .filter((entry) => sameDay(entry.transaction_date, date))
        .reduce((sum, entry) => sum + entry.amount, 0);
      value = previous + salesTotal - expensesTotal;
    } else if (metric === "margen") {
      const dayRevenue = sales
        .filter((entry) => sameDay(entry.transaction_date, date))
        .reduce((sum, entry) => sum + entry.amount, 0);
      const dayCost = expenses
        .filter((entry) => sameDay(entry.transaction_date, date))
        .reduce((sum, entry) => sum + entry.amount, 0);
      // null cuando no hay ventas: el chart muestra un gap real en vez de interpolar
      value = dayRevenue > 0 ? ((dayRevenue - dayCost) / dayRevenue) * 100 : null;
    } else {
      // stock: usa score_stock del historial real; fallback a coseno si aún no hay historial
      if (scoreHistory && scoreHistory.length > 0) {
        const nearest = scoreHistory.reduce((best, s) => {
          const d = Math.abs(new Date(s.created_at).getTime() - date.getTime());
          const bd = Math.abs(new Date(best.created_at).getTime() - date.getTime());
          return d < bd ? s : best;
        });
        value = nearest.score_stock;
      } else {
        value = totalStock + Math.cos(index / 2.3) * 18;
      }
    }

    const change = value !== null && previous !== 0
      ? ((value - previous) / Math.abs(previous)) * 100
      : 0;
    previous = value ?? previous;

    return {
      label:
        granularity === "monthly"
          ? date.toLocaleDateString("es-AR", { month: "short" })
          : date.toLocaleDateString("es-AR", { day: "2-digit", month: "2-digit" }),
      value,
      change,
    };
  });
}

export function buildComparisonSeries(
  compareBy: CompareOption["value"],
  sales: SaleEntryResponse[],
  expenses: ExpenseEntryResponse[],
  products: ProductResponse[],
): Array<{ label: string; value: number }> {
  const productMap = new Map(products.map((product) => [product.id, product]));
  const buckets = new Map<string, number>();

  if (compareBy === "categoria") {
    for (const sale of sales) {
      const category = productMap.get(sale.product_id ?? "")?.category || "Sin categoría";
      buckets.set(category, (buckets.get(category) ?? 0) + sale.amount);
    }
  } else if (compareBy === "proveedor") {
    for (const expense of expenses) {
      const supplier = expense.supplier_name?.trim() || "Sin proveedor";
      buckets.set(supplier, (buckets.get(supplier) ?? 0) + expense.amount);
    }
  } else if (compareBy === "metodo") {
    for (const sale of sales) {
      const method = normalizePaymentMethod(sale.payment_method);
      buckets.set(method, (buckets.get(method) ?? 0) + sale.amount);
    }
  } else {
    for (const sale of sales) {
      const weekday = WEEKDAY_LABELS[new Date(sale.transaction_date).getDay()] ?? "—";
      buckets.set(weekday, (buckets.get(weekday) ?? 0) + sale.amount);
    }
  }

  return [...buckets.entries()]
    .map(([label, value]) => ({ label, value }))
    .sort((a, b) => b.value - a.value)
    .slice(0, 6);
}

export function buildDistributionSeries(
  distribution: DistributionOption["value"],
  sales: SaleEntryResponse[],
  expenses: ExpenseEntryResponse[],
  products: ProductResponse[],
): Array<{ name: string; value: number }> {
  if (distribution === "cajaMetodo") {
    return buildCashBreakdown(sales).rows.map((row) => ({
      name: row.method,
      value: row.amount,
    }));
  }

  if (distribution === "stockEstado") {
    return buildStockStatuses(products).map((row) => ({
      name: row.label,
      value: row.count,
    }));
  }

  if (distribution === "comprasProveedor") {
    return buildSupplierSummaries(expenses).slice(0, 5).map((row) => ({
      name: row.name,
      value: row.totalPurchased,
    }));
  }

  const productMap = new Map(products.map((product) => [product.id, product]));
  const buckets = new Map<string, number>();

  for (const sale of sales) {
    const category = productMap.get(sale.product_id ?? "")?.category || "Sin categoría";
    buckets.set(category, (buckets.get(category) ?? 0) + sale.amount);
  }

  return [...buckets.entries()].map(([name, value]) => ({ name, value }));
}

export function getMarginToneClass(tone: MarginCategoryRow["tone"]): string {
  if (tone === "green") return "text-vektor-teal";
  if (tone === "amber") return "text-vektor-amber";
  return "text-vektor-red";
}
