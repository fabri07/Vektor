"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { Building2, ChevronDown, ChevronRight, Package, Tag, Users, Wallet } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { Select } from "@/components/ui/Select";
import { Tooltip } from "@/components/ui/Tooltip";
import type { CustomerResponse } from "@/services/customers.service";
import type { ExpenseEntryResponse } from "@/services/expenses.service";
import type { ProductResponse } from "@/services/products.service";
import type { SaleEntryResponse } from "@/services/sales.service";
import type { SupplierResponse } from "@/services/suppliers.service";
import {
  buildCashBreakdown,
  buildCustomerBreakdown,
  buildMarginBreakdown,
  buildProductPurchases,
  buildStockStatuses,
  buildSupplierBreakdown,
  formatARS,
  formatARSCompact,
  formatPercent,
  formatShortDate,
  getMarginToneClass,
  KPI_TOOLTIP_COPY,
  type VolumeSummary,
} from "@/features/dashboard/dashboardData";

interface Props {
  sales: SaleEntryResponse[];
  expenses: ExpenseEntryResponse[];
  products: ProductResponse[];
  suppliers: SupplierResponse[];
  customers: CustomerResponse[];
  loading?: boolean;
}

function CardEmptyState({
  icon: Icon,
  title,
  description,
  cta,
  ctaHref,
}: {
  icon: LucideIcon;
  title: string;
  description: string;
  cta: string;
  ctaHref: string;
}) {
  return (
    <div className="mt-5 flex flex-col items-center gap-3 rounded-xl border border-dashed border-vektor-border bg-vektor-surface/40 py-8 text-center">
      <Icon className="h-8 w-8 text-vektor-muted" />
      <div>
        <p className="text-sm font-medium text-vektor-body">{title}</p>
        <p className="mt-1 text-xs text-vektor-muted">{description}</p>
      </div>
      <Link
        href={ctaHref}
        className="inline-flex items-center rounded-full border border-vektor-border bg-vektor-surface px-4 py-2 text-xs font-medium text-vektor-body transition-all duration-200 hover:border-vektor-blue hover:text-vektor-white"
      >
        {cta} →
      </Link>
    </div>
  );
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

function DetailTile({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl border border-vektor-border bg-vektor-surface p-4">
      <p className="text-xs font-semibold uppercase tracking-[0.16em] text-vektor-muted">{label}</p>
      <p className="mt-2 text-xl font-semibold text-vektor-white">{value}</p>
    </div>
  );
}

/**
 * Desplegable + tarjetas de detalle para un breakdown de volumen
 * (proveedores, clientes o productos comprados). Maneja su propia selección.
 */
function VolumeBreakdownPanel({
  entries,
  placeholder,
  volumeLabel,
  lastLabel,
  ticketLabel,
  volumeTooltip,
}: {
  entries: VolumeSummary[];
  placeholder: string;
  volumeLabel: string;
  lastLabel: string;
  ticketLabel: string;
  volumeTooltip: string;
}) {
  const [selectedKey, setSelectedKey] = useState<string>("");
  const options = entries.map((entry) => ({
    value: entry.key,
    label: `${entry.name} — ${formatARSCompact(entry.total)}`,
  }));
  const detail = entries.find((entry) => entry.key === selectedKey) ?? entries[0];
  if (!detail) return null;

  return (
    <>
      <div className="mt-4">
        <Select
          options={options}
          value={detail.key}
          onChange={setSelectedKey}
          placeholder={placeholder}
        />
      </div>
      <div className="mt-5 grid gap-3 sm:grid-cols-2">
        <div className="rounded-xl border border-vektor-border bg-vektor-surface p-4">
          <Tooltip content={volumeTooltip}>
            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-vektor-muted">
              {volumeLabel}
            </p>
          </Tooltip>
          <p className="mt-2 text-xl font-semibold text-vektor-white">{formatARS(detail.total)}</p>
        </div>
        <DetailTile label={lastLabel} value={formatShortDate(detail.lastDate)} />
        <DetailTile label={ticketLabel} value={formatARS(detail.average)} />
        <DetailTile label="Operaciones" value={String(detail.count)} />
      </div>
    </>
  );
}

export function DashboardSummaryCards({
  sales,
  expenses,
  products,
  suppliers,
  customers,
  loading,
}: Props) {
  const router = useRouter();
  const [cashOpen, setCashOpen] = useState(false);

  const cash = useMemo(() => buildCashBreakdown(sales), [sales]);
  const margins = useMemo(() => buildMarginBreakdown(products), [products]);
  const stockStatuses = useMemo(() => buildStockStatuses(products), [products]);
  const productPurchases = useMemo(
    () => buildProductPurchases(expenses, products),
    [expenses, products],
  );
  const supplierBreakdown = useMemo(
    () => buildSupplierBreakdown(expenses, suppliers),
    [expenses, suppliers],
  );
  const customerBreakdown = useMemo(
    () => buildCustomerBreakdown(sales, customers),
    [sales, customers],
  );

  if (loading) {
    return (
      <div className="grid gap-4 xl:grid-cols-2">
        {Array.from({ length: 5 }).map((_, index) => (
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
              {sales.length === 0 ? "—" : formatARS(cash.total)}
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

        {sales.length === 0 ? (
          <CardEmptyState
            icon={Wallet}
            title="Sin movimientos de caja"
            description="Registrá una venta o ingreso para ver el estado de tu caja."
            cta="Registrar venta"
            ctaHref="/chat"
          />
        ) : cashOpen ? (
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
        {margins.length === 0 ? (
          <CardEmptyState
            icon={Tag}
            title="Sin productos para calcular margen"
            description="Agregá productos para ver los márgenes por categoría."
            cta="Registrar compra"
            ctaHref="/ingestion"
          />
        ) : (
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
        )}
      </article>

      <article className="vektor-card p-5">
        <Tooltip content={KPI_TOOLTIP_COPY.Stock}>
          <h2 className="text-lg font-semibold text-vektor-white">Stock</h2>
        </Tooltip>
        {products.length === 0 ? (
          <CardEmptyState
            icon={Package}
            title="Todavía no cargaste productos"
            description="Registrá una compra para ver el estado de tu inventario."
            cta="Registrar compra"
            ctaHref="/ingestion"
          />
        ) : (
          <>
            <div className="mt-5 flex flex-wrap gap-3">
              {stockStatuses.map((status) => {
                const isStub = status.id === "incoming";
                return (
                  <Tooltip key={status.id} content={status.description}>
                    <button
                      type="button"
                      disabled={isStub}
                      onClick={() => {
                        if (!isStub) router.push(`/products?stock=${status.id}`);
                      }}
                      aria-label={`Filtrar productos por ${status.label}`}
                      className={`inline-flex items-center gap-2 rounded-full border border-vektor-border bg-vektor-surface px-4 py-2 text-sm font-medium text-vektor-body${isStub ? " cursor-not-allowed opacity-50" : ""}`}
                    >
                      <span className={`h-2.5 w-2.5 rounded-full ${status.colorClass.split(" ")[0]}`} />
                      <span>{status.label}</span>
                      <span className="text-vektor-white">{status.count}</span>
                    </button>
                  </Tooltip>
                );
              })}
            </div>
            {productPurchases.length > 0 ? (
              <div className="mt-5">
                <Tooltip content="Cuánto compraste de cada producto en el período. Sirve para ver dónde concentrás la inversión en mercadería.">
                  <p className="text-sm font-medium text-vektor-body">Compras por producto</p>
                </Tooltip>
                <VolumeBreakdownPanel
                  entries={productPurchases}
                  placeholder="Elegí un producto"
                  volumeLabel="Volumen de compra"
                  lastLabel="Última compra"
                  ticketLabel="Ticket promedio"
                  volumeTooltip="Total invertido en comprar este producto durante el período."
                />
              </div>
            ) : (
              <p className="mt-4 text-sm leading-6 text-vektor-muted">
                Tocá un estado para abrir la vista de inventario ya filtrada cuando haya productos para revisar.
              </p>
            )}
          </>
        )}
      </article>

      <article className="vektor-card p-5">
        <div className="flex items-start justify-between gap-4">
          <Tooltip content={KPI_TOOLTIP_COPY.Proveedores}>
            <h2 className="text-lg font-semibold text-vektor-white">Proveedores</h2>
          </Tooltip>
          {supplierBreakdown.length > 0 ? (
            <Link href="/suppliers" className="text-sm font-medium text-vektor-body">
              Ver todos →
            </Link>
          ) : null}
        </div>

        {supplierBreakdown.length === 0 ? (
          <CardEmptyState
            icon={Building2}
            title="Sin compras registradas"
            description="Registrá una compra para ver el volumen por proveedor acá."
            cta="Registrar compra"
            ctaHref="/ingestion"
          />
        ) : (
          <VolumeBreakdownPanel
            entries={supplierBreakdown}
            placeholder="Elegí un proveedor"
            volumeLabel="Volumen de compra"
            lastLabel="Último pedido"
            ticketLabel="Ticket promedio"
            volumeTooltip="Total de dinero que le compraste a este proveedor en el período seleccionado."
          />
        )}
      </article>

      <article className="vektor-card p-5">
        <div className="flex items-start justify-between gap-4">
          <Tooltip content="Clientes: quiénes concentran tus ventas. Las ventas sin cliente cargado aparecen como 'Cliente no identificado'.">
            <h2 className="text-lg font-semibold text-vektor-white">Clientes</h2>
          </Tooltip>
          {customerBreakdown.length > 0 ? (
            <Link href="/customers" className="text-sm font-medium text-vektor-body">
              Ver todos →
            </Link>
          ) : null}
        </div>

        {customerBreakdown.length === 0 ? (
          <CardEmptyState
            icon={Users}
            title="Sin ventas registradas"
            description="Registrá una venta para ver el volumen por cliente acá."
            cta="Registrar venta"
            ctaHref="/chat"
          />
        ) : (
          <VolumeBreakdownPanel
            entries={customerBreakdown}
            placeholder="Elegí un cliente"
            volumeLabel="Volumen vendido"
            lastLabel="Última venta"
            ticketLabel="Ticket promedio"
            volumeTooltip="Total que te compró este cliente en el período seleccionado."
          />
        )}
      </article>
    </div>
  );
}
