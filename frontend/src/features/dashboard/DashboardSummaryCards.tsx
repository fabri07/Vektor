"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { ChevronDown, ChevronRight, Truck } from "lucide-react";
import { Select } from "@/components/ui/Select";
import { Tooltip } from "@/components/ui/Tooltip";
import type { ExpenseEntryResponse } from "@/services/expenses.service";
import type { ProductResponse } from "@/services/products.service";
import type { SaleEntryResponse } from "@/services/sales.service";
import {
  buildCashBreakdown,
  buildMarginBreakdown,
  buildStockStatuses,
  buildSupplierSummaries,
  formatARS,
  formatARSCompact,
  formatPercent,
  formatShortDate,
  getMarginToneClass,
  KPI_TOOLTIP_COPY,
} from "@/features/dashboard/dashboardData";

interface Props {
  sales: SaleEntryResponse[];
  expenses: ExpenseEntryResponse[];
  products: ProductResponse[];
  loading?: boolean;
}

function SkeletonCard() {
  return (
    <div className="vektor-card animate-pulse p-5">
      <div className="h-4 w-28 rounded bg-vektor-surface" />
      <div className="mt-5 h-8 w-32 rounded bg-vektor-surface" />
      <div className="mt-4 h-24 rounded-xl bg-vektor-surface" />
    </div>
  );
}

export function DashboardSummaryCards({ sales, expenses, products, loading }: Props) {
  const router = useRouter();
  const [cashOpen, setCashOpen] = useState(false);
  const [selectedSupplier, setSelectedSupplier] = useState<string>("");

  const cash = useMemo(() => buildCashBreakdown(sales), [sales]);
  const margins = useMemo(() => buildMarginBreakdown(products), [products]);
  const stockStatuses = useMemo(() => buildStockStatuses(products), [products]);
  const suppliers = useMemo(() => buildSupplierSummaries(expenses), [expenses]);

  const supplierOptions = suppliers.map((supplier) => ({
    value: supplier.name,
    label: `${supplier.name} — ${formatARSCompact(supplier.totalPurchased)}`,
  }));

  const selectedSupplierDetail =
    suppliers.find((supplier) => supplier.name === selectedSupplier) ?? suppliers[0];

  if (loading) {
    return (
      <div className="grid gap-4 xl:grid-cols-2">
        {Array.from({ length: 4 }).map((_, index) => (
          <SkeletonCard key={index} />
        ))}
      </div>
    );
  }

  return (
    <div className="grid gap-4 xl:grid-cols-2">
      <article className="vektor-card p-5">
        <div className="flex items-start justify-between gap-4">
          <div>
            <Tooltip content={KPI_TOOLTIP_COPY.Caja}>
              <h2 className="text-lg font-semibold text-vektor-white">Caja</h2>
            </Tooltip>
            <p className="mt-2 text-3xl font-semibold text-vektor-white">
              {formatARS(cash.total)}
            </p>
          </div>
          <button
            type="button"
            onClick={() => setCashOpen((value) => !value)}
            aria-label={cashOpen ? "Ocultar desglose de caja" : "Mostrar desglose de caja"}
            className="rounded-full border border-vektor-border bg-vektor-surface p-2 text-vektor-body"
          >
            {cashOpen ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
          </button>
        </div>

        {cashOpen ? (
          <div className="mt-5 space-y-3">
            {cash.rows.map((row) => (
              <div key={row.method} className={`rounded-xl border border-vektor-border bg-vektor-surface p-3 border-l-4 ${row.colorClass}`}>
                <div className="flex items-center justify-between gap-3">
                  <div className="flex items-center gap-2">
                    {row.method === "Pagaré" ? (
                      <Tooltip content="Documento de deuda a cobrar en una fecha futura acordada.">
                        <span className="text-sm font-medium text-vektor-white">{row.method}</span>
                      </Tooltip>
                    ) : (
                      <span className="text-sm font-medium text-vektor-white">{row.method}</span>
                    )}
                  </div>
                  <div className="text-right">
                    <p className="text-sm font-semibold text-vektor-white">{formatARS(row.amount)}</p>
                    <p className="text-xs text-vektor-muted">{formatPercent(row.percentage)}</p>
                  </div>
                </div>
                <div className="mt-3 h-2 rounded-full bg-vektor-night">
                  <div
                    className="h-2 rounded-full bg-gradient-to-r from-vektor-blue to-vektor-teal"
                    style={{ width: `${row.percentage}%` }}
                  />
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p className="mt-4 text-sm leading-6 text-vektor-muted">
            Abrí el desglose para ver cómo se reparte la caja entre efectivo, tarjetas, cheque y pagaré.
          </p>
        )}
      </article>

      <article className="vektor-card p-5">
        <Tooltip content={KPI_TOOLTIP_COPY.Margen}>
          <h2 className="text-lg font-semibold text-vektor-white">Margen</h2>
        </Tooltip>
        <div className="mt-5 space-y-3">
          {margins.map((row) => (
            <Tooltip
              key={row.category}
              content={`Margen promedio de ${row.category}: cuánto ganás por cada peso vendido en esta categoría.`}
            >
              <div className="flex items-center justify-between rounded-xl border border-vektor-border bg-vektor-surface px-4 py-3">
                <span className="text-sm font-medium text-vektor-body">{row.category}</span>
                <span className={`text-base font-semibold ${getMarginToneClass(row.tone)}`}>
                  {formatPercent(row.margin)}
                </span>
              </div>
            </Tooltip>
          ))}
        </div>
      </article>

      <article className="vektor-card p-5">
        <Tooltip content={KPI_TOOLTIP_COPY.Stock}>
          <h2 className="text-lg font-semibold text-vektor-white">Stock</h2>
        </Tooltip>
        <div className="mt-5 flex flex-wrap gap-3">
          {stockStatuses.map((status) => (
            <Tooltip key={status.id} content={status.description}>
              <button
                type="button"
                onClick={() => {
                  if (status.id === "incoming") return;
                  router.push(`/products?stock=${status.id}`);
                }}
                aria-label={`Filtrar productos por ${status.label}`}
                className="inline-flex items-center gap-2 rounded-full border border-vektor-border bg-vektor-surface px-4 py-2 text-sm font-medium text-vektor-body"
              >
                <span className={`h-2.5 w-2.5 rounded-full ${status.colorClass.split(" ")[0]}`} />
                <span>{status.label}</span>
                <span className="text-vektor-white">{status.count}</span>
              </button>
            </Tooltip>
          ))}
        </div>
        <p className="mt-4 text-sm leading-6 text-vektor-muted">
          Tocá un estado para abrir la vista de inventario ya filtrada cuando haya productos para revisar.
        </p>
      </article>

      <article className="vektor-card p-5">
        <div className="flex items-start justify-between gap-4">
          <div>
            <Tooltip content={KPI_TOOLTIP_COPY.Proveedores}>
              <h2 className="text-lg font-semibold text-vektor-white">Proveedores</h2>
            </Tooltip>
            {suppliers.length > 10 ? (
              <p className="mt-1 text-sm text-vektor-muted">Top 5 por volumen de compra</p>
            ) : null}
          </div>
          {suppliers.length > 10 ? (
            <Link href="/expenses" className="text-sm font-medium text-vektor-body">
              Ver todos →
            </Link>
          ) : null}
        </div>

        <div className="mt-4">
          <Select
            options={supplierOptions}
            value={selectedSupplierDetail?.name}
            onChange={setSelectedSupplier}
            placeholder="Elegí un proveedor"
          />
        </div>

        {selectedSupplierDetail ? (
          <div className="mt-5 grid gap-3 sm:grid-cols-2">
            <div className="rounded-xl border border-vektor-border bg-vektor-surface p-4">
              <Tooltip content="Total de dinero que le compraste a este proveedor en el período seleccionado.">
                <p className="text-xs font-semibold uppercase tracking-[0.16em] text-vektor-muted">
                  Volumen de compra
                </p>
              </Tooltip>
              <p className="mt-2 text-xl font-semibold text-vektor-white">
                {formatARS(selectedSupplierDetail.totalPurchased)}
              </p>
            </div>
            <div className="rounded-xl border border-vektor-border bg-vektor-surface p-4">
              <p className="text-xs font-semibold uppercase tracking-[0.16em] text-vektor-muted">
                Ultimo pedido
              </p>
              <p className="mt-2 text-xl font-semibold text-vektor-white">
                {formatShortDate(selectedSupplierDetail.lastOrderDate)}
              </p>
            </div>
            <div className="rounded-xl border border-vektor-border bg-vektor-surface p-4">
              <p className="text-xs font-semibold uppercase tracking-[0.16em] text-vektor-muted">
                Ticket promedio
              </p>
              <p className="mt-2 text-xl font-semibold text-vektor-white">
                {formatARS(selectedSupplierDetail.averageOrderValue)}
              </p>
            </div>
            <div className="rounded-xl border border-vektor-border bg-vektor-surface p-4">
              <p className="text-xs font-semibold uppercase tracking-[0.16em] text-vektor-muted">
                Condiciones / vencidas
              </p>
              <div className="mt-2 flex items-center justify-between gap-2">
                <span className="text-base font-semibold text-vektor-white">
                  {selectedSupplierDetail.paymentTerms}
                </span>
                <span className="inline-flex items-center gap-2 rounded-full bg-vektor-night px-3 py-1 text-xs font-medium text-vektor-body">
                  <Truck className="h-3.5 w-3.5" />
                  {selectedSupplierDetail.overdueInvoicesCount} vencidas
                </span>
              </div>
            </div>
          </div>
        ) : (
          <div className="mt-5 rounded-xl border border-dashed border-vektor-border bg-vektor-surface/60 p-5 text-sm text-vektor-muted">
            No hay proveedores cargados todavia.
          </div>
        )}
      </article>
    </div>
  );
}
