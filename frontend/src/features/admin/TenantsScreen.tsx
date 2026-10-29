"use client";

import { useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { PageWrapper } from "@/components/layout/PageWrapper";
import { EmptyState } from "@/components/ui/EmptyState";
import { Select } from "@/components/ui/Select";
import { SmartTable } from "@/components/ui/SmartTable";
import type { SmartColumn } from "@/components/ui/SmartTable";
import { adminTenantsService } from "@/services/adminTenants.service";
import type { AdminTenantListItem } from "@/services/adminTenants.service";
import {
  ESTADOS_DE_CUENTA,
  entero,
  fechaCorta,
  rotuloDeCuenta,
  rotuloDeSuscripcion,
} from "./format";

const PAGE_SIZE = 25;

const FILTRO_ESTADO = [{ value: "", label: "Todos los estados" }, ...ESTADOS_DE_CUENTA];

const columns: SmartColumn<AdminTenantListItem>[] = [
  {
    key: "display_name",
    header: "Negocio",
    render: (v, row) => (
      <Link
        href={`/admin/tenants/${row.tenant_id}`}
        className="font-medium text-vk-text-primary underline-offset-2 hover:underline"
      >
        {String(v)}
        {row.is_demo ? " (demo)" : ""}
      </Link>
    ),
    csvValue: (v) => String(v ?? ""),
  },
  { key: "vertical_code", header: "Rubro", render: (v) => String(v ?? "—") },
  {
    key: "account_status",
    header: "Cuenta",
    render: (v) => rotuloDeCuenta(String(v)),
    csvValue: (v) => String(v ?? ""),
  },
  {
    key: "subscription_plan",
    header: "Plan",
    render: (v) => String(v ?? "—"),
  },
  {
    key: "subscription_status",
    header: "Suscripción",
    render: (v) => rotuloDeSuscripcion((v as string | null) ?? null),
    csvValue: (v) => String(v ?? ""),
  },
  {
    key: "current_period_end",
    header: "Pago hasta",
    render: (v) => fechaCorta(v as string | null),
    csvValue: (v) => String(v ?? ""),
  },
  {
    key: "users_count",
    header: "Usuarios",
    render: (v) => entero(Number(v ?? 0)),
    csvValue: (v) => String(Number(v ?? 0)),
  },
  {
    key: "last_login_at",
    header: "Último ingreso",
    render: (v) => fechaCorta(v as string | null),
    csvValue: (v) => String(v ?? ""),
  },
  {
    key: "tokens_30d",
    header: "Tokens (30 d)",
    render: (v) => entero(Number(v ?? 0)),
    csvValue: (v) => String(Number(v ?? 0)),
  },
];

/** Monitor de plataforma: todos los negocios, con su estado de cuenta y de suscripción. */
export function TenantsScreen() {
  const [page, setPage] = useState(1);
  const [q, setQ] = useState("");
  const [estado, setEstado] = useState("");

  const { data, isLoading, isError } = useQuery({
    queryKey: ["admin-tenants", page, q, estado],
    queryFn: () =>
      adminTenantsService.list({
        page,
        page_size: PAGE_SIZE,
        q: q.trim() || undefined,
        account_status: estado || undefined,
      }),
    placeholderData: (previo) => previo,
  });

  const total = data?.total ?? 0;
  const paginas = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const filtros = (
    <div className="flex flex-wrap items-center gap-2">
      <input
        type="search"
        value={q}
        onChange={(e) => {
          setQ(e.target.value);
          setPage(1);
        }}
        placeholder="Buscar negocio…"
        aria-label="Buscar negocio"
        className="h-9 rounded-md border border-vk-border-w bg-vk-surface-w px-3 text-sm text-vk-text-primary"
      />
      <Select
        options={FILTRO_ESTADO}
        value={estado}
        onChange={(v) => {
          setEstado(v);
          setPage(1);
        }}
      />
    </div>
  );

  return (
    <PageWrapper title="Negocios" actions={filtros}>
      <p className="text-xs text-vk-text-muted">
        «Cuenta» la decide el soporte; «Suscripción» sigue el ciclo comercial. Son controles
        distintos. «Último ingreso» es el último login conocido, no actividad.
      </p>
      {isLoading ? (
        <div className="rounded-lg border border-vk-border-w bg-vk-surface-w p-10 text-center text-sm text-vk-text-muted">
          Cargando negocios…
        </div>
      ) : isError ? (
        <div className="rounded-lg border border-vk-border-w bg-vk-surface-w p-10">
          <EmptyState
            title="No se pudieron cargar los negocios"
            description="Ocurrió un error al consultar la plataforma. Intentá nuevamente."
          />
        </div>
      ) : (
        <>
          <SmartTable
            columns={columns}
            data={data?.items ?? []}
            exportFilename="vektor-negocios"
            emptyMessage="No hay negocios con esos filtros."
          />
          <div className="flex items-center justify-between text-sm text-vk-text-muted">
            <span>
              {entero(total)} negocios · página {page} de {paginas}
            </span>
            <div className="flex gap-2">
              <button
                type="button"
                disabled={page <= 1}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                className="rounded-md border border-vk-border-w px-3 py-1 disabled:opacity-40"
              >
                Anterior
              </button>
              <button
                type="button"
                disabled={page >= paginas}
                onClick={() => setPage((p) => p + 1)}
                className="rounded-md border border-vk-border-w px-3 py-1 disabled:opacity-40"
              >
                Siguiente
              </button>
            </div>
          </div>
        </>
      )}
    </PageWrapper>
  );
}
