"use client";

import { useMemo } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import { PageWrapper } from "@/components/layout/PageWrapper";
import { Badge } from "@/components/ui/Badge";
import { EmptyState } from "@/components/ui/EmptyState";
import { SmartTable } from "@/components/ui/SmartTable";
import { ContactCommunication } from "@/features/communication/ContactCommunication";
import { customersService } from "@/services/customers.service";
import { salesService, type SaleEntryResponse } from "@/services/sales.service";
import { productsService } from "@/services/products.service";
import {
  customerTypeLabel,
  ivaConditionLabel,
  whatsappDigits,
} from "@/lib/fiscal";
import { paymentLabel } from "@/lib/suppliers";
import { formatDate, formatDateTime } from "@/lib/datetime";
import { formatARS } from "@/features/dashboard/dashboardData";

interface SummaryCard {
  label: string;
  value: string;
}

export default function CustomerDetailPage() {
  const params = useParams<{ id: string }>();
  const customerId = params.id;

  const {
    data: customer,
    isLoading: customerLoading,
    isError: customerError,
  } = useQuery({
    queryKey: ["customer", customerId],
    queryFn: () => customersService.getCustomer(customerId),
    staleTime: 2 * 60 * 1000,
  });

  const {
    data: balance,
    isLoading: balanceLoading,
    isError: balanceError,
  } = useQuery({
    queryKey: ["customer-balance", customerId],
    queryFn: () => customersService.getCustomerBalance(customerId),
    enabled: !!customer && !customer.is_sentinel,
    staleTime: 60 * 1000,
  });

  const {
    data: sales = [],
    isLoading: salesLoading,
    isError: salesError,
  } = useQuery({
    queryKey: ["customer-sales", customerId],
    queryFn: () => salesService.getAllEntries({ customer_id: customerId }),
    staleTime: 60 * 1000,
  });

  const { data: products = [] } = useQuery({
    queryKey: ["products-list"],
    queryFn: () => productsService.getAllProducts(),
    staleTime: 5 * 60 * 1000,
  });

  const productNameById = useMemo(() => {
    const map = new Map<string, string>();
    for (const p of products) map.set(p.id, p.name);
    return map;
  }, [products]);

  const summary = useMemo<SummaryCard[]>(() => {
    if (sales.length === 0) return [];
    const total = sales.reduce((s, e) => s + e.amount, 0);
    const count = sales.length;
    const avgTicket = total / count;
    const lastSale = sales.reduce<string | null>((latest, e) => {
      if (!latest || e.transaction_date > latest) return e.transaction_date;
      return latest;
    }, null);

    // Método de pago más usado (moda).
    const payCounts = new Map<string, number>();
    for (const e of sales) {
      payCounts.set(e.payment_method, (payCounts.get(e.payment_method) ?? 0) + 1);
    }
    let topPayment = "";
    let topCount = 0;
    for (const [method, c] of payCounts) {
      if (c > topCount) {
        topCount = c;
        topPayment = method;
      }
    }

    return [
      { label: "Total comprado", value: formatARS(total) },
      { label: "Compras", value: String(count) },
      { label: "Ticket promedio", value: formatARS(avgTicket) },
      { label: "Última compra", value: lastSale ? formatDate(lastSale) : "—" },
      { label: "Pago más usado", value: topPayment ? paymentLabel(topPayment) : "—" },
    ];
  }, [sales]);

  const historyRows = useMemo(
    () =>
      sales
        .slice()
        .sort((a, b) => (a.transaction_date < b.transaction_date ? 1 : -1))
        .map((e: SaleEntryResponse) => ({
          ...e,
          _product: e.product_id ? productNameById.get(e.product_id) ?? "—" : "—",
        })),
    [sales, productNameById],
  );

  if (customerLoading) {
    return (
      <PageWrapper title="Cliente">
        <div className="space-y-3">
          <div className="h-28 animate-pulse rounded-lg border border-vk-border-w bg-vk-surface-w" />
          {[...Array<number>(4)].map((_, i) => (
            <div
              key={i}
              className="h-12 animate-pulse rounded-lg border border-vk-border-w bg-vk-surface-w"
            />
          ))}
        </div>
      </PageWrapper>
    );
  }

  if (customerError || !customer) {
    return (
      <PageWrapper title="Cliente">
        <p className="rounded-lg border border-vk-danger/20 bg-vk-danger-bg px-4 py-3 text-sm text-vk-danger">
          No se pudo cargar el cliente.{" "}
          <Link href="/customers" className="underline">
            Volver a Clientes
          </Link>
          .
        </p>
      </PageWrapper>
    );
  }

  const emailValue = customer.email?.trim() ?? "";
  const phoneValue = customer.phone?.trim() ?? "";
  const waDigits = whatsappDigits(phoneValue);
  const docValue = customer.cuit?.trim()
    ? `CUIT ${customer.cuit.trim()}`
    : customer.dni?.trim()
      ? `DNI ${customer.dni.trim()}`
      : "—";
  const addressLine = [
    customer.address?.trim(),
    customer.locality?.trim(),
    customer.province?.trim(),
    customer.postal_code?.trim() ? `CP ${customer.postal_code.trim()}` : "",
  ]
    .filter(Boolean)
    .join(", ");

  return (
    <PageWrapper title={customer.name}>
      <Link
        href="/customers"
        className="inline-flex w-fit items-center gap-1.5 text-sm text-vk-text-secondary transition-colors hover:text-vk-text-primary"
      >
        <ArrowLeft className="h-4 w-4" />
        Volver a Clientes
      </Link>

      {/* Resumen comercial */}
      {summary.length > 0 && (
        <section className="grid grid-cols-2 gap-3 md:grid-cols-5">
          {summary.map((card) => (
            <div
              key={card.label}
              className="rounded-lg border border-vk-border-w bg-vk-surface-w p-3"
            >
              <p className="text-xs text-vk-text-muted">{card.label}</p>
              <p className="mt-1 font-display text-lg font-semibold text-vk-text-primary">
                {card.value}
              </p>
            </div>
          ))}
        </section>
      )}

      {/* Ficha fiscal + contacto. El centinela "Local" no tiene datos de contacto
          ni ficha fiscal: mostramos una tarjeta explicativa en su lugar. */}
      {customer.is_sentinel ? (
        <section className="rounded-lg border border-vk-border-w bg-vk-surface-w p-4">
          <p className="text-sm text-vk-text-secondary">
            <span className="font-medium text-vk-text-primary">
              Cliente del sistema.
            </span>{" "}
            Agrupa las ventas realizadas sin cliente identificado.
          </p>
        </section>
      ) : (
      <section className="rounded-lg border border-vk-border-w bg-vk-surface-w p-4">
        <div className="grid grid-cols-2 gap-3 text-sm md:grid-cols-3">
          <div>
            <p className="text-xs text-vk-text-muted">Tipo</p>
            <p className="text-vk-text-primary">{customerTypeLabel(customer.customer_type)}</p>
          </div>
          {customer.last_name?.trim() ? (
            <div>
              <p className="text-xs text-vk-text-muted">Apellido</p>
              <p className="text-vk-text-primary">{customer.last_name}</p>
            </div>
          ) : null}
          <div>
            <p className="text-xs text-vk-text-muted">CUIT / DNI</p>
            <p className="text-vk-text-primary">{docValue}</p>
          </div>
          <div>
            <p className="text-xs text-vk-text-muted">Condición IVA</p>
            <p className="text-vk-text-primary">{ivaConditionLabel(customer.iva_condition)}</p>
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
            <p className="text-xs text-vk-text-muted">Celular</p>
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
          {addressLine ? (
            <div className="col-span-2 md:col-span-3">
              <p className="text-xs text-vk-text-muted">Dirección de entrega</p>
              <p className="text-vk-text-primary">{addressLine}</p>
            </div>
          ) : null}
          {customer.birthday?.trim() ? (
            <div>
              <p className="text-xs text-vk-text-muted">Cumpleaños</p>
              <p className="text-vk-text-primary">{formatDate(customer.birthday)}</p>
            </div>
          ) : null}
          <div>
            <p className="text-xs text-vk-text-muted">Estado</p>
            <p>
              {customer.is_active ? (
                <Badge variant="success">Activo</Badge>
              ) : (
                <Badge variant="default">Inactivo</Badge>
              )}
            </p>
          </div>
          {customer.notes?.trim() ? (
            <div className="col-span-2 md:col-span-3">
              <p className="text-xs text-vk-text-muted">Notas / Preferencias</p>
              <p className="text-vk-text-primary">{customer.notes}</p>
            </div>
          ) : null}
        </div>

        <div className="mt-4 border-t border-vk-border-w pt-4">
          <ContactCommunication
            recipientType="customer"
            recipientId={customer.id}
            email={customer.email}
            phone={customer.phone}
          />
        </div>
      </section>
      )}

      {/* Panel de saldo — solo para clientes reales (no centinela) */}
      {!customer.is_sentinel && (
        <section className="space-y-3">
          <h2 className="font-display text-base font-semibold text-vk-text-primary">
            Cuenta corriente y fiado
          </h2>
          {balanceLoading ? (
            <div className="h-24 animate-pulse rounded-lg border border-vk-border-w bg-vk-surface-w" />
          ) : balanceError ? (
            <p className="rounded-lg border border-vk-danger/20 bg-vk-danger-bg px-4 py-3 text-sm text-vk-danger">
              No se pudo cargar el saldo del cliente.
            </p>
          ) : balance ? (
            <div className="rounded-lg border border-vk-border-w bg-vk-surface-w p-4">
              {balance.over_limit && (
                <p className="mb-3 rounded border border-vk-danger/30 bg-vk-danger-bg px-3 py-2 text-sm font-medium text-vk-danger">
                  Supera el límite de crédito
                </p>
              )}
              <div className="grid grid-cols-2 gap-4 text-sm md:grid-cols-4">
                <div>
                  <p className="text-xs text-vk-text-muted">Fiado pendiente</p>
                  <p className="mt-1 font-display text-lg font-semibold text-vk-text-primary">
                    {formatARS(balance.balance)}
                  </p>
                </div>
                <div>
                  <p className="text-xs text-vk-text-muted">Total a cuenta corriente</p>
                  <p className="mt-1 font-display text-base font-semibold text-vk-text-primary">
                    {formatARS(balance.total_account)}
                  </p>
                </div>
                <div>
                  <p className="text-xs text-vk-text-muted">Total cobrado</p>
                  <p className="mt-1 font-display text-base font-semibold text-vk-text-primary">
                    {formatARS(balance.total_paid)}
                  </p>
                </div>
                <div>
                  <p className="text-xs text-vk-text-muted">Límite de crédito</p>
                  <p className="mt-1 font-display text-base font-semibold text-vk-text-primary">
                    {balance.credit_limit !== null
                      ? formatARS(balance.credit_limit)
                      : "Sin límite configurado"}
                  </p>
                </div>
              </div>
            </div>
          ) : null}
        </section>
      )}

      {/* Historial de compras */}
      <section className="space-y-3">
        <h2 className="font-display text-base font-semibold text-vk-text-primary">
          Historial de compras
        </h2>

        {salesLoading ? (
          <div className="space-y-2">
            {[...Array<number>(4)].map((_, i) => (
              <div
                key={i}
                className="h-12 animate-pulse rounded-lg border border-vk-border-w bg-vk-surface-w"
              />
            ))}
          </div>
        ) : salesError ? (
          <p className="rounded-lg border border-vk-danger/20 bg-vk-danger-bg px-4 py-3 text-sm text-vk-danger">
            Error al cargar el historial de compras. Recargá la página.
          </p>
        ) : historyRows.length === 0 ? (
          <EmptyState
            title="Sin compras registradas"
            description="Cuando registres ventas con este cliente, aparecerán acá."
          />
        ) : (
          <SmartTable
            columns={HISTORY_COLUMNS}
            data={historyRows as unknown as Record<string, unknown>[]}
            exportFilename={`vektor-cliente-${customer.name}`}
          />
        )}
      </section>
    </PageWrapper>
  );
}

const HISTORY_COLUMNS = [
  {
    key: "transaction_date",
    header: "Fecha",
    hideable: true,
    render: (v: unknown) => (
      <span className="text-vk-text-secondary">{formatDateTime(v)}</span>
    ),
    csvValue: (v: unknown) => formatDateTime(v),
  },
  {
    key: "_product",
    header: "Producto",
    hideable: true,
    render: (v: unknown) => (
      <span className="font-medium text-vk-text-primary">{String(v ?? "—")}</span>
    ),
    csvValue: (v: unknown) => String(v ?? ""),
  },
  {
    key: "quantity",
    header: "Cantidad",
    hideable: true,
    render: (v: unknown) => <span className="text-vk-text-primary">{Number(v)}</span>,
    csvValue: (v: unknown) => String(Number(v)),
  },
  {
    key: "payment_method",
    header: "Método de pago",
    hideable: true,
    render: (v: unknown) => {
      const s = String(v ?? "").trim();
      return s ? paymentLabel(s) : "—";
    },
    csvValue: (v: unknown) => {
      const s = String(v ?? "").trim();
      return s ? paymentLabel(s) : "";
    },
  },
  {
    key: "amount",
    header: "Monto",
    hideable: true,
    render: (v: unknown) => (
      <span className="text-vk-text-primary">{formatARS(Number(v))}</span>
    ),
    csvValue: (v: unknown) => String(Number(v)),
  },
];
