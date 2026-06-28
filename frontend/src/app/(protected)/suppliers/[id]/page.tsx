"use client";

import { useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, FileText } from "lucide-react";
import { PageWrapper } from "@/components/layout/PageWrapper";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { EmptyState } from "@/components/ui/EmptyState";
import { SmartTable } from "@/components/ui/SmartTable";
import { ContactCommunication } from "@/features/communication/ContactCommunication";
import { ReceiptModal } from "@/features/suppliers/ReceiptModal";
import {
  suppliersService,
  type SupplierProductPurchase,
} from "@/services/suppliers.service";
import { paymentLabel, whatsappDigits } from "@/lib/suppliers";
import { formatDateTime } from "@/lib/datetime";
import { formatARS } from "@/features/dashboard/dashboardData";
import { useToastStore } from "@/stores/toastStore";

const PRODUCT_COLUMNS = [
  {
    key: "name",
    header: "Producto",
    hideable: true,
    render: (v: unknown) => (
      <span className="font-medium text-vk-text-primary">{String(v)}</span>
    ),
    csvValue: (v: unknown) => String(v ?? ""),
  },
  {
    key: "last_purchase_at",
    header: "Última compra",
    hideable: true,
    render: (v: unknown) => (
      <span className="text-vk-text-secondary">{formatDateTime(v)}</span>
    ),
    csvValue: (v: unknown) => formatDateTime(v),
  },
  {
    key: "total_qty",
    header: "Cantidad",
    hideable: true,
    render: (v: unknown) => (
      <span className="text-vk-text-primary">{Number(v)}</span>
    ),
    csvValue: (v: unknown) => String(Number(v)),
  },
  {
    key: "unit_price",
    header: "Precio unit.",
    hideable: true,
    render: (v: unknown) => (
      <span className="text-vk-text-primary">{formatARS(Number(v))}</span>
    ),
    csvValue: (v: unknown) => String(Number(v)),
  },
];

export default function SupplierDetailPage() {
  const params = useParams<{ id: string }>();
  const supplierId = params.id;
  const queryClient = useQueryClient();
  const toast = useToastStore((s) => s.add);
  const [uploadingReceipt, setUploadingReceipt] = useState(false);

  const {
    data: supplier,
    isLoading: supplierLoading,
    isError: supplierError,
  } = useQuery({
    queryKey: ["supplier", supplierId],
    queryFn: () => suppliersService.getSupplier(supplierId),
    staleTime: 2 * 60 * 1000,
  });

  const {
    data: products = [],
    isLoading: productsLoading,
    isError: productsError,
  } = useQuery({
    queryKey: ["supplier-products", supplierId],
    queryFn: () => suppliersService.getSupplierProducts(supplierId),
    staleTime: 60 * 1000,
  });

  if (supplierLoading) {
    return (
      <PageWrapper title="Proveedor">
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

  if (supplierError || !supplier) {
    return (
      <PageWrapper title="Proveedor">
        <p className="rounded-lg border border-vk-danger/20 bg-vk-danger-bg px-4 py-3 text-sm text-vk-danger">
          No se pudo cargar el proveedor.{" "}
          <Link href="/suppliers" className="underline">
            Volver a Proveedores
          </Link>
          .
        </p>
      </PageWrapper>
    );
  }

  const emailValue = supplier.email?.trim() ?? "";
  const phoneValue = supplier.phone?.trim() ?? "";
  const waDigits = whatsappDigits(phoneValue);

  const totalPurchased = products.reduce(
    (s: number, p: SupplierProductPurchase) => s + p.total_qty * p.unit_price,
    0,
  );

  return (
    <PageWrapper
      title={supplier.name}
      actions={
        <Button size="sm" variant="secondary" onClick={() => setUploadingReceipt(true)}>
          <FileText className="h-4 w-4" />
          Cargar remito
        </Button>
      }
    >
      <Link
        href="/suppliers"
        className="inline-flex w-fit items-center gap-1.5 text-sm text-vk-text-secondary transition-colors hover:text-vk-text-primary"
      >
        <ArrowLeft className="h-4 w-4" />
        Volver a Proveedores
      </Link>

      {/* Ficha de contacto */}
      <section className="rounded-lg border border-vk-border-w bg-vk-surface-w p-4">
        <div className="grid grid-cols-2 gap-3 text-sm md:grid-cols-3">
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
          {!supplier.is_sentinel && supplier.catalog_url?.trim() ? (
            <div>
              <p className="text-xs text-vk-text-muted">Catálogo</p>
              <a
                href={supplier.catalog_url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-vk-blue underline-offset-2 hover:underline"
              >
                Ver catálogo
              </a>
            </div>
          ) : null}
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
            <div className="col-span-2 md:col-span-3">
              <p className="text-xs text-vk-text-muted">Notas</p>
              <p className="text-vk-text-primary">{supplier.notes}</p>
            </div>
          ) : null}
        </div>

        <div className="mt-4 border-t border-vk-border-w pt-4">
          <ContactCommunication
            recipientType="supplier"
            recipientId={supplier.id}
            email={supplier.email}
            phone={supplier.phone}
          />
        </div>
      </section>

      {/* Productos comprados */}
      <section className="space-y-3">
        <div className="flex items-center justify-between">
          <h2 className="font-display text-base font-semibold text-vk-text-primary">
            Productos comprados
          </h2>
          {products.length > 0 && (
            <span className="text-sm text-vk-text-secondary">
              Total comprado · {formatARS(totalPurchased)}
            </span>
          )}
        </div>

        {productsLoading ? (
          <div className="space-y-2">
            {[...Array<number>(4)].map((_, i) => (
              <div
                key={i}
                className="h-12 animate-pulse rounded-lg border border-vk-border-w bg-vk-surface-w"
              />
            ))}
          </div>
        ) : productsError ? (
          <p className="rounded-lg border border-vk-danger/20 bg-vk-danger-bg px-4 py-3 text-sm text-vk-danger">
            Error al cargar los productos comprados. Recargá la página.
          </p>
        ) : products.length === 0 ? (
          <EmptyState
            title="Sin compras registradas"
            description="Cargá el primer remito de este proveedor con el botón 'Cargar remito'."
          />
        ) : (
          <SmartTable
            columns={PRODUCT_COLUMNS}
            data={products as unknown as Record<string, unknown>[]}
            exportFilename={`vektor-proveedor-${supplier.name}`}
          />
        )}
      </section>

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
    </PageWrapper>
  );
}
